/*
 * Copyright (c) 2015 Cisco and/or its affiliates.
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at:
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
/*
 * pg_stream.c: packet generator streams
 *
 * Copyright (c) 2008 Eliot Dresselhaus
 *
 * Permission is hereby granted, free of charge, to any person obtaining
 * a copy of this software and associated documentation files (the
 * "Software"), to deal in the Software without restriction, including
 * without limitation the rights to use, copy, modify, merge, publish,
 * distribute, sublicense, and/or sell copies of the Software, and to
 * permit persons to whom the Software is furnished to do so, subject to
 * the following conditions:
 *
 * The above copyright notice and this permission notice shall be
 * included in all copies or substantial portions of the Software.
 *
 *  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
 *  EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 *  MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 *  NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
 *  LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
 *  OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
 *  WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
 */

#include <vnet/vnet.h>
#include <vnet/pg/pg.h>
#include <vnet/ethernet/ethernet.h>
#include <vnet/ip/ip.h>
#include <vnet/mpls/mpls.h>
#include <vnet/devices/devices.h>

/* Mark stream active or inactive. */
void
pg_stream_enable_disable (pg_main_t * pg, pg_stream_t * s, int want_enabled)
{
  vlib_main_t *vm;
  vnet_main_t *vnm = vnet_get_main ();
  pg_interface_t *pi = pool_elt_at_index (pg->interfaces, s->pg_if_index);

  want_enabled = want_enabled != 0;

  if (pg_stream_is_enabled (s) == want_enabled)
    /* No change necessary. */
    return;

  if (want_enabled)
    s->n_packets_generated = 0;

  /* Toggle enabled flag. */
  s->flags ^= PG_STREAM_FLAGS_IS_ENABLED;

  ASSERT (!pool_is_free (pg->streams, s));

  vec_validate (pg->enabled_streams, s->worker_index);
  pg->enabled_streams[s->worker_index] =
    clib_bitmap_set (pg->enabled_streams[s->worker_index], s - pg->streams,
		     want_enabled);

  if (want_enabled)
    {
      vnet_hw_interface_set_flags (vnm, pi->hw_if_index,
				   VNET_HW_INTERFACE_FLAG_LINK_UP);

      vnet_sw_interface_set_flags (vnm, pi->sw_if_index,
				   VNET_SW_INTERFACE_FLAG_ADMIN_UP);
    }

  if (vlib_num_workers ())
    vm = vlib_get_worker_vlib_main (s->worker_index);
  else
    vm = vlib_get_main ();

  vlib_node_set_state (vm, pg_input_node.index,
		       (clib_bitmap_is_zero
			(pg->enabled_streams[s->worker_index]) ?
			VLIB_NODE_STATE_DISABLED : VLIB_NODE_STATE_POLLING));

  s->packet_accumulator = 0;
  s->time_last_generate = 0;
}

static u8 *
format_pg_output_trace (u8 * s, va_list * va)
{
  CLIB_UNUSED (vlib_main_t * vm) = va_arg (*va, vlib_main_t *);
  CLIB_UNUSED (vlib_node_t * node) = va_arg (*va, vlib_node_t *);
  pg_output_trace_t *t = va_arg (*va, pg_output_trace_t *);
  u32 indent = format_get_indent (s);

  s = format (s, "%Ubuffer 0x%x: %U", format_white_space, indent,
	      t->buffer_index, format_vnet_buffer_no_chain, &t->buffer);

  if (t->mode == PG_MODE_IP4)
    s = format (s, "\n%U%U", format_white_space, indent, format_ip4_header,
		t->buffer.pre_data, sizeof (t->buffer.pre_data));
  else if (t->mode == PG_MODE_IP6)
    s = format (s, "\n%U%U", format_white_space, indent, format_ip6_header,
		t->buffer.pre_data, sizeof (t->buffer.pre_data));
  else
    s = format (s, "\n%U%U", format_white_space, indent,
		format_ethernet_header_with_length, t->buffer.pre_data,
		sizeof (t->buffer.pre_data));

  return s;
}

static u8 *
format_pg_interface_name (u8 * s, va_list * args)
{
  pg_main_t *pg = &pg_main;
  u32 if_index = va_arg (*args, u32);
  pg_interface_t *pi;

  pi = pool_elt_at_index (pg->interfaces, if_index);
  s = format (s, "pg%d", pi->id);

  return s;
}

static clib_error_t *
pg_interface_admin_up_down (vnet_main_t * vnm, u32 hw_if_index, u32 flags)
{
  u32 hw_flags = 0;

  if (flags & VNET_SW_INTERFACE_FLAG_ADMIN_UP)
    hw_flags = VNET_HW_INTERFACE_FLAG_LINK_UP;

  vnet_hw_interface_set_flags (vnm, hw_if_index, hw_flags);

  return 0;
}

static int
pg_mac_address_cmp (const mac_address_t * m1, const mac_address_t * m2)
{
  return (!mac_address_cmp (m1, m2));
}

static clib_error_t *
pg_add_del_mac_address (vnet_hw_interface_t * hi,
			const u8 * address, u8 is_add)
{
  pg_main_t *pg = &pg_main;

  if (ethernet_address_cast (address))
    {
      mac_address_t mac;
      pg_interface_t *pi;

      pi = pool_elt_at_index (pg->interfaces, hi->dev_instance);

      mac_address_from_bytes (&mac, address);
      if (is_add)
	vec_add1 (pi->allowed_mcast_macs, mac);
      else
	{
	  u32 pos = vec_search_with_function (pi->allowed_mcast_macs, &mac,
					      pg_mac_address_cmp);
	  if (~0 != pos)
	    vec_del1 (pi->allowed_mcast_macs, pos);
	}
    }
  return (NULL);
}

VNET_DEVICE_CLASS (pg_dev_class) = {
  .name = "pg",
  .tx_function = pg_output,
  .format_device_name = format_pg_interface_name,
  .format_tx_trace = format_pg_output_trace,
  .admin_up_down_function = pg_interface_admin_up_down,
  .mac_addr_add_del_function = pg_add_del_mac_address,
};

static u8 *
pg_build_rewrite (vnet_main_t * vnm,
		  u32 sw_if_index,
		  vnet_link_t link_type, const void *dst_address)
{
  u8 *rewrite = NULL;
  u16 *h;

  vec_validate (rewrite, sizeof (*h) - 1);
  h = (u16 *) rewrite;
  h[0] = clib_host_to_net_u16 (vnet_link_to_l3_proto (link_type));

  return (rewrite);
}

VNET_HW_INTERFACE_CLASS (pg_interface_class,static) = {
  .name = "Packet generator",
  .build_rewrite = pg_build_rewrite,
};

static u32
pg_eth_flag_change (vnet_main_t * vnm, vnet_hw_interface_t * hi, u32 flags)
{
  /* nothing for now */
  return 0;
}

void
pg_interface_enable_disable_coalesce (pg_interface_t * pi, u8 enable,
				      u32 tx_node_index)
{
  if (enable)
    {
      gro_flow_table_init (&pi->flow_table, 1 /* is_l2 */ ,
			   tx_node_index);
      pi->coalesce_enabled = 1;
    }
  else
    {
      pi->coalesce_enabled = 0;
      gro_flow_table_free (pi->flow_table);
    }
}

u8 *
format_pg_tun_tx_trace (u8 *s, va_list *args)
{
  CLIB_UNUSED (vlib_main_t * vm) = va_arg (*args, vlib_main_t *);
  CLIB_UNUSED (vlib_node_t * node) = va_arg (*args, vlib_node_t *);

  s = format (s, "PG: tunnel (no-encap)");
  return s;
}

VNET_HW_INTERFACE_CLASS (pg_tun_hw_interface_class) = {
  .name = "PG-tun",
  //.format_header = format_gre_header_with_length,
  //.unformat_header = unformat_gre_header,
  .build_rewrite = NULL,
  //.update_adjacency = gre_update_adj,
  .flags = VNET_HW_INTERFACE_CLASS_FLAG_P2P,
  .tx_hash_fn_type = VNET_HASH_FN_TYPE_IP,
};

u32
pg_interface_add_or_get (pg_main_t *pg, pg_interface_args_t *args)
{
  vnet_main_t *vnm = vnet_get_main ();
  pg_interface_t *pi;
  vnet_hw_interface_t *hi;
  uword *p;
  u32 i;

  p = hash_get (pg->if_index_by_if_id, args->if_id);

  if (p)
    {
      return p[0];
    }
  else
    {
      pool_get (pg->interfaces, pi);
      i = pi - pg->interfaces;
      pi->id = args->if_id;
      pi->mode = args->mode;

      switch (pi->mode)
	{
	case PG_MODE_ETHERNET:
	  {
	    vnet_eth_interface_registration_t eir = { 0 };
	    if (!args->hw_addr_set)
	      ethernet_mac_address_generate (args->hw_addr.bytes);
	    clib_memcpy (pi->hw_addr.bytes, args->hw_addr.bytes, 6);
	    eir.dev_class_index = pg_dev_class.index;
	    eir.dev_instance = i;
	    eir.address = pi->hw_addr.bytes;
	    eir.cb.flag_change = pg_eth_flag_change;
	    pi->hw_if_index = vnet_eth_register_interface (vnm, &eir);
	    break;
	  }
	case PG_MODE_IP4:
	case PG_MODE_IP6:
	  pi->hw_if_index = vnet_register_interface (
	    vnm, pg_dev_class.index, i, pg_tun_hw_interface_class.index, i);
	  break;
	}
      hi = vnet_get_hw_interface (vnm, pi->hw_if_index);
      if (args->flags & PG_INTERFACE_FLAG_GSO)
	{
	  vnet_hw_if_set_caps (vnm, pi->hw_if_index, VNET_HW_IF_CAP_TCP_GSO);
	  pi->gso_enabled = 1;
	  pi->gso_size = args->gso_size;
	  if (args->flags & PG_INTERFACE_FLAG_GRO_COALESCE)
	    {
	      pg_interface_enable_disable_coalesce (pi, 1, hi->tx_node_index);
	    }
	}
      pi->sw_if_index = hi->sw_if_index;

      hash_set (pg->if_index_by_if_id, pi->id, i);

      vec_validate (pg->if_index_by_sw_if_index, hi->sw_if_index);
      pg->if_index_by_sw_if_index[hi->sw_if_index] = i;

      if (vlib_num_workers ())
	{
	  pi->lockp = clib_mem_alloc_aligned (CLIB_CACHE_LINE_BYTES,
					      CLIB_CACHE_LINE_BYTES);
	  *pi->lockp = 0;
	}
    }

  return i;
}

int
pg_interface_delete (u32 sw_if_index)
{
  vnet_main_t *vnm = vnet_get_main ();
  pg_main_t *pm = &pg_main;
  pg_interface_t *pi;
  vnet_hw_interface_t *hw;
  uword *p;

  hw = vnet_get_sup_hw_interface_api_visible_or_null (vnm, sw_if_index);
  if (hw == NULL || pg_dev_class.index != hw->dev_class_index)
    return VNET_API_ERROR_INVALID_SW_IF_INDEX;

  pi = pool_elt_at_index (pm->interfaces, hw->dev_instance);

  vnet_hw_interface_set_flags (vnm, pi->hw_if_index, 0);
  vnet_sw_interface_set_flags (vnm, pi->sw_if_index, 0);

  if (pi->mode == PG_MODE_ETHERNET)
    ethernet_delete_interface (vnm, pi->hw_if_index);
  else
    vnet_delete_hw_interface (vnm, pi->hw_if_index);

  pi->hw_if_index = ~0;

  if (pi->coalesce_enabled)
    pg_interface_enable_disable_coalesce (pi, 0, ~0);

  if (vlib_num_workers ())
    {
      clib_mem_free ((void *) pi->lockp);
      pi->lockp = 0;
    }

  vec_del1 (pm->if_index_by_sw_if_index, sw_if_index);
  p = hash_get (pm->if_index_by_if_id, pi->id);
  if (p)
    hash_unset (pm->if_index_by_if_id, pi->id);

  clib_memset (pi, 0, sizeof (*pi));
  pool_put (pm->interfaces, pi);
  return 0;
}

static void
do_edit (pg_stream_t * stream,
	 pg_edit_group_t * g, pg_edit_t * e, uword want_commit)
{
  u32 i, i0, i1, mask, n_bits_left;
  u8 *v, *s, *m;

  i0 = e->lsb_bit_offset / BITS (u8);

  /* Make space for edit in value and mask. */
  vec_validate (g->fixed_packet_data, i0);
  vec_validate (g->fixed_packet_data_mask, i0);

  if (e->type != PG_EDIT_FIXED)
    {
      switch (e->type)
	{
	case PG_EDIT_RANDOM:
	case PG_EDIT_INCREMENT:
	  e->last_increment_value = pg_edit_get_value (e, PG_EDIT_LO);
	  break;

	default:
	  break;
	}

      if (want_commit)
	{
	  ASSERT (e->type != PG_EDIT_INVALID_TYPE);
	  vec_add1 (g->non_fixed_edits, e[0]);
	}
      return;
    }

  s = g->fixed_packet_data;
  m = g->fixed_packet_data_mask;

  n_bits_left = e->n_bits;
  i0 = e->lsb_bit_offset / BITS (u8);
  i1 = e->lsb_bit_offset % BITS (u8);

  v = e->values[PG_EDIT_LO];
  i = pg_edit_n_alloc_bytes (e) - 1;

  /* Odd low order bits?. */
  if (i1 != 0 && n_bits_left > 0)
    {
      u32 n = clib_min (n_bits_left, BITS (u8) - i1);

      mask = pow2_mask (n) << i1;

      ASSERT (i0 < vec_len (s));
      ASSERT (i < vec_len (v));
      ASSERT ((v[i] & ~mask) == 0);

      s[i0] |= v[i] & mask;
      m[i0] |= mask;

      i0--;
      i--;
      n_bits_left -= n;
    }

  /* Even bytes. */
  while (n_bits_left >= 8)
    {
      ASSERT (i0 < vec_len (s));
      ASSERT (i < vec_len (v));

      s[i0] = v[i];
      m[i0] = ~0;

      i0--;
      i--;
      n_bits_left -= 8;
    }

  /* Odd high order bits. */
  if (n_bits_left > 0)
    {
      mask = pow2_mask (n_bits_left);

      ASSERT (i0 < vec_len (s));
      ASSERT (i < vec_len (v));
      ASSERT ((v[i] & ~mask) == 0);

      s[i0] |= v[i] & mask;
      m[i0] |= mask;
    }

  if (want_commit)
    pg_edit_free (e);
}

void
pg_edit_group_get_fixed_packet_data (pg_stream_t * s,
				     u32 group_index,
				     void *packet_data,
				     void *packet_data_mask)
{
  pg_edit_group_t *g = pg_stream_get_group (s, group_index);
  pg_edit_t *e;

  vec_foreach (e, g->edits) do_edit (s, g, e, /* want_commit */ 0);

  clib_memcpy_fast (packet_data, g->fixed_packet_data,
		    vec_len (g->fixed_packet_data));
  clib_memcpy_fast (packet_data_mask, g->fixed_packet_data_mask,
		    vec_len (g->fixed_packet_data_mask));
}

static void
perform_fixed_edits (pg_stream_t * s)
{
  pg_edit_group_t *g;
  pg_edit_t *e;
  word i;

  for (i = vec_len (s->edit_groups) - 1; i >= 0; i--)
    {
      g = vec_elt_at_index (s->edit_groups, i);
      vec_foreach (e, g->edits) do_edit (s, g, e, /* want_commit */ 1);

      /* All edits have either been performed or added to
         g->non_fixed_edits.  So, we can delete the vector. */
      vec_free (g->edits);
    }

  vec_free (s->fixed_packet_data_mask);
  vec_free (s->fixed_packet_data);
  vec_foreach (g, s->edit_groups)
  {
    int i;
    g->start_byte_offset = vec_len (s->fixed_packet_data);

    /* Relocate and copy non-fixed edits from group to stream. */
    vec_foreach (e, g->non_fixed_edits)
      e->lsb_bit_offset += g->start_byte_offset * BITS (u8);

    for (i = 0; i < vec_len (g->non_fixed_edits); i++)
      ASSERT (g->non_fixed_edits[i].type != PG_EDIT_INVALID_TYPE);

    vec_add (s->non_fixed_edits,
	     g->non_fixed_edits, vec_len (g->non_fixed_edits));
    vec_free (g->non_fixed_edits);

    vec_add (s->fixed_packet_data,
	     g->fixed_packet_data, vec_len (g->fixed_packet_data));
    vec_add (s->fixed_packet_data_mask,
	     g->fixed_packet_data_mask, vec_len (g->fixed_packet_data_mask));
  }
}

void
pg_stream_add (pg_main_t * pg, pg_stream_t * s_init)
{
  vlib_main_t *vm = vlib_get_main ();
  pg_stream_t *s;
  uword *p;

  if (!pg->stream_index_by_name)
    pg->stream_index_by_name
      = hash_create_vec (0, sizeof (s->name[0]), sizeof (uword));

  /* Delete any old stream with the same name. */
  if (s_init->name
      && (p = hash_get_mem (pg->stream_index_by_name, s_init->name)))
    {
      pg_stream_del (pg, p[0]);
    }

  pool_get (pg->streams, s);
  s[0] = s_init[0];

  /* Give it a name. */
  if (!s->name)
    s->name = format (0, "stream%d", s - pg->streams);
  else
    s->name = vec_dup (s->name);

  hash_set_mem (pg->stream_index_by_name, s->name, s - pg->streams);

  /* Get fixed part of buffer data. */
  if (s->edit_groups)
    perform_fixed_edits (s);

  /* Determine packet size. */
  switch (s->packet_size_edit_type)
    {
    case PG_EDIT_INCREMENT:
    case PG_EDIT_RANDOM:
      if (s->min_packet_bytes == s->max_packet_bytes)
	s->packet_size_edit_type = PG_EDIT_FIXED;
      break;

    default:
      /* Get packet size from fixed edits. */
      s->packet_size_edit_type = PG_EDIT_FIXED;
      if (!s->replay_packet_templates)
	s->min_packet_bytes = s->max_packet_bytes =
	  vec_len (s->fixed_packet_data);
      break;
    }

  s->last_increment_packet_size = s->min_packet_bytes;

  {
    int n;

    s->buffer_bytes = vlib_buffer_get_default_data_size (vm);
    n = s->max_packet_bytes / s->buffer_bytes;
    n += (s->max_packet_bytes % s->buffer_bytes) != 0;

    vec_resize (s->buffer_indices, n);
  }

  pg_interface_args_t args = {
    .if_id = s->if_id,
    .mode = PG_MODE_ETHERNET,
    .flags = 0,	      /* gso_enabled and coalesce_enabled */
    .gso_size = 0,    /* gso_size */
    .hw_addr_set = 0, /* mac address set */
  };

  /* Find an interface to use. */
  s->pg_if_index = pg_interface_add_or_get (pg, &args);

  if (s->sw_if_index[VLIB_RX] == ~0)
    {
      pg_interface_t *pi = pool_elt_at_index (pg->interfaces, s->pg_if_index);
      /*
       * Default the RX interface if unset. It's a bad mistake to
       * set [VLIB_TX] prior to ip lookup, since the ip lookup code
       * interprets [VLIB_TX] as a fib index...
       */
      s->sw_if_index[VLIB_RX] = pi->sw_if_index;
    }
  else if (vec_len (pg->if_index_by_sw_if_index) <= s->sw_if_index[VLIB_RX])
    {
      vec_validate (pg->if_index_by_sw_if_index, s->sw_if_index[VLIB_RX]);
      pg->if_index_by_sw_if_index[s->sw_if_index[VLIB_RX]] = s->pg_if_index;
    }

  /* Connect the graph. */
  s->next_index = vlib_node_add_next (vm, device_input_node.index,
				      s->node_index);
}

void
pg_stream_del (pg_main_t * pg, uword index)
{
  pg_stream_t *s;

  s = pool_elt_at_index (pg->streams, index);

  pg_stream_enable_disable (pg, s, /* want_enabled */ 0);
  hash_unset_mem (pg->stream_index_by_name, s->name);

  pg_stream_free (s);
  pool_put (pg->streams, s);
}

void
pg_stream_change (pg_main_t * pg, pg_stream_t * s)
{
  /* Determine packet size. */
  switch (s->packet_size_edit_type)
    {
    case PG_EDIT_INCREMENT:
    case PG_EDIT_RANDOM:
      if (s->min_packet_bytes == s->max_packet_bytes)
	s->packet_size_edit_type = PG_EDIT_FIXED;
    case PG_EDIT_FIXED:
      break;

    default:
      /* Get packet size from fixed edits. */
      s->packet_size_edit_type = PG_EDIT_FIXED;
      if (!s->replay_packet_templates)
	s->min_packet_bytes = s->max_packet_bytes =
	  vec_len (s->fixed_packet_data);
      break;
    }

  s->last_increment_packet_size = s->min_packet_bytes;
}


/*
 * fd.io coding-style-patch-verification: ON
 *
 * Local Variables:
 * eval: (c-set-style "gnu")
 * End:
 */
