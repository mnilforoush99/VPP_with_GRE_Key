/*
 * node.c: gre packet processing
 *
 * Copyright (c) 2012 Cisco and/or its affiliates.
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

#include <vlib/vlib.h>
#include <vnet/pg/pg.h>
#include <gre/gre.h>
#include <vnet/mpls/mpls.h>
#include <vppinfra/sparse_vec.h>
#include <vppinfra/hash.h>

#define foreach_gre_input_next                                                \
  _ (PUNT, "error-punt")                                                      \
  _ (DROP, "error-drop")                                                      \
  _ (ETHERNET_INPUT, "ethernet-input")                                        \
  _ (IP4_INPUT, "ip4-input")                                                  \
  _ (IP6_INPUT, "ip6-input")                                                  \
  _ (MPLS_INPUT, "mpls-input")

typedef enum
{
#define _(s, n) GRE_INPUT_NEXT_##s,
  foreach_gre_input_next
#undef _
    GRE_INPUT_N_NEXT,
} gre_input_next_t;

typedef struct
{
  u32 tunnel_id;
  u32 length;
  ip46_address_t src;
  ip46_address_t dst;
} gre_rx_trace_t;

extern u8 *format_gre_rx_trace (u8 *s, va_list *args);

#ifndef CLIB_MARCH_VARIANT
u8 *
format_gre_rx_trace (u8 *s, va_list *args)
{
  CLIB_UNUSED (vlib_main_t * vm) = va_arg (*args, vlib_main_t *);
  CLIB_UNUSED (vlib_node_t * node) = va_arg (*args, vlib_node_t *);
  gre_rx_trace_t *t = va_arg (*args, gre_rx_trace_t *);

  s = format (s, "GRE: tunnel %d len %d src %U dst %U", t->tunnel_id,
	      clib_net_to_host_u16 (t->length), format_ip46_address, &t->src,
	      IP46_TYPE_ANY, format_ip46_address, &t->dst, IP46_TYPE_ANY);
  return s;
}
#endif /* CLIB_MARCH_VARIANT */

typedef struct
{
  /* Sparse vector mapping gre protocol in network byte order
     to next index. */
  u16 *next_by_protocol;
} gre_input_runtime_t;

always_inline void
gre_trace (vlib_main_t *vm, vlib_node_runtime_t *node, vlib_buffer_t *b,
	   u32 tun_sw_if_index, const ip6_header_t *ip6,
	   const ip4_header_t *ip4, int is_ipv6)
{
  gre_rx_trace_t *tr = vlib_add_trace (vm, node, b, sizeof (*tr));
  tr->tunnel_id = tun_sw_if_index;
  if (is_ipv6)
    {
      tr->length = ip6->payload_length;
      tr->src.ip6.as_u64[0] = ip6->src_address.as_u64[0];
      tr->src.ip6.as_u64[1] = ip6->src_address.as_u64[1];
      tr->dst.ip6.as_u64[0] = ip6->dst_address.as_u64[0];
      tr->dst.ip6.as_u64[1] = ip6->dst_address.as_u64[1];
    }
  else
    {
      tr->length = ip4->length;
      tr->src.as_u64[0] = tr->src.as_u64[1] = 0;
      tr->dst.as_u64[0] = tr->dst.as_u64[1] = 0;
      tr->src.ip4.as_u32 = ip4->src_address.as_u32;
      tr->dst.ip4.as_u32 = ip4->dst_address.as_u32;
    }
}

always_inline void
gre_tunnel_get (const gre_main_t *gm, vlib_node_runtime_t *node,
		vlib_buffer_t *b, u16 *next, const gre_tunnel_key_t *key,
		gre_tunnel_key_t *cached_key, u32 *tun_sw_if_index,
		u32 *cached_tun_sw_if_index, int is_ipv6)
{
  // debug 4
  clib_warning ("Key structure details:");
  clib_warning ("- Total size: %u", sizeof (*key));
  if (!is_ipv6)
    {
      clib_warning ("- Field offsets in gtk_v4:");
      clib_warning ("  gtk_src: %lu",
		    offsetof (struct gre_tunnel_key4_t_, gtk_src));
      clib_warning ("  gtk_dst: %lu",
		    offsetof (struct gre_tunnel_key4_t_, gtk_dst));
      clib_warning ("  gtk_common: %lu",
		    offsetof (struct gre_tunnel_key4_t_, gtk_common));

      clib_warning ("- Field values:");
      clib_warning ("  gtk_src: %U", format_ip4_address, &key->gtk_v4.gtk_src);
      clib_warning ("  gtk_dst: %U", format_ip4_address, &key->gtk_v4.gtk_dst);
      clib_warning ("  gtk_common.fib_index: %u",
		    key->gtk_v4.gtk_common.fib_index);
      clib_warning ("  gtk_common.gre_key: %u",
		    key->gtk_v4.gtk_common.gre_key);
      clib_warning ("  gtk_common.type: %u", key->gtk_v4.gtk_common.type);
      clib_warning ("  gtk_common.mode: %u", key->gtk_v4.gtk_common.mode);
    }
  // debug 1
  if (!is_ipv6)
    {
      clib_warning ("Key details - key struct size: %u", sizeof (key->gtk_v4));
      clib_warning ("Key v4 details: %u", key->gtk_v4);
    }
  // end debug 1 print

  //debug IPv6
  // In gre_tunnel_get, right before the hash lookup
  if (is_ipv6) {
    clib_warning("IPv6 tunnel lookup - key memory: ");
  for (int i = 0; i < sizeof(gre_tunnel_key6_t); i += 16) {
    clib_warning("  bytes %2d-%2d: %02x%02x%02x%02x %02x%02x%02x%02x %02x%02x%02x%02x %02x%02x%02x%02x",
                i, i+15,
                ((u8*)&key->gtk_v6)[i],
                ((u8*)&key->gtk_v6)[i+1],
                ((u8*)&key->gtk_v6)[i+2],
                ((u8*)&key->gtk_v6)[i+3],
                ((u8*)&key->gtk_v6)[i+4],
                ((u8*)&key->gtk_v6)[i+5],
                ((u8*)&key->gtk_v6)[i+6],
                ((u8*)&key->gtk_v6)[i+7],
                ((u8*)&key->gtk_v6)[i+8],
                ((u8*)&key->gtk_v6)[i+9],
                ((u8*)&key->gtk_v6)[i+10],
                ((u8*)&key->gtk_v6)[i+11],
                ((u8*)&key->gtk_v6)[i+12],
                ((u8*)&key->gtk_v6)[i+13],
                ((u8*)&key->gtk_v6)[i+14],
                ((u8*)&key->gtk_v6)[i+15]);
  }
  
  // Simply check if there are any entries in the hash table
  u32 count = hash_elts(gm->tunnel_by_key6);
  clib_warning("Total IPv6 tunnels in hash: %d", count);
  
  // Instead of trying to iterate the hash, let's just print our lookup details
  clib_warning("Looking up IPv6 tunnel - src: %U dst: %U fib: %d type: %d key: %u",
              format_ip6_address, &key->gtk_v6.gtk_src,
              format_ip6_address, &key->gtk_v6.gtk_dst,
              key->gtk_v6.gtk_common.fib_index,
              key->gtk_v6.gtk_common.type,
              key->gtk_v6.gtk_common.gre_key);
  }

  const uword *p;
  p = is_ipv6 ? hash_get_mem (gm->tunnel_by_key6, &key->gtk_v6) :
		hash_get_mem (gm->tunnel_by_key4, &key->gtk_v4);
  // debug 2
  clib_warning ("Tunnel lookup result: %d", p ? 1 : 0);
  // debug 5
  clib_warning ("Lookup key memory: %U", format_hex_bytes, &key->gtk_v4,
		sizeof (gre_tunnel_key4_t));
  clib_warning (
    "Lookup hash value: %u",
    hash_memory ((void *) &key->gtk_v4, sizeof (gre_tunnel_key4_t), 0));
  if (PREDICT_FALSE (!p))
    {
      *next = GRE_INPUT_NEXT_DROP;
      b->error = node->errors[GRE_ERROR_NO_SUCH_TUNNEL];
      *tun_sw_if_index = ~0;
    }
  else
    {
      const gre_tunnel_t *tun;
      tun = pool_elt_at_index (gm->tunnels, *p);
      *cached_tun_sw_if_index = *tun_sw_if_index = tun->sw_if_index;
      if (is_ipv6)
	cached_key->gtk_v6 = key->gtk_v6;
      else
	cached_key->gtk_v4 = key->gtk_v4;
    }
}

always_inline uword
gre_input (vlib_main_t *vm, vlib_node_runtime_t *node, vlib_frame_t *frame,
	   const int is_ipv6)
{
  gre_main_t *gm = &gre_main;
  u32 *from, n_left_from;
  vlib_buffer_t *bufs[VLIB_FRAME_SIZE], **b = bufs;
  u16 nexts[VLIB_FRAME_SIZE], *next = nexts;
  u16 cached_protocol = ~0;
  u32 cached_next_index = SPARSE_VEC_INVALID_INDEX;
  u32 cached_tun_sw_if_index = ~0;
  gre_tunnel_key_t cached_key;

  from = vlib_frame_vector_args (frame);
  n_left_from = frame->n_vectors;
  vlib_get_buffers (vm, from, bufs, n_left_from);

  if (is_ipv6)
    clib_memset (&cached_key.gtk_v6, 0xff, sizeof (cached_key.gtk_v6));
  else
    clib_memset (&cached_key.gtk_v4, 0xff, sizeof (cached_key.gtk_v4));

  while (n_left_from >= 2)
    {
      const ip6_header_t *ip6[2];
      const ip4_header_t *ip4[2];
      const gre_header_t *gre[2];
      u32 nidx[2];
      next_info_t ni[2];
      u8 type[2];
      u16 version[2];
      u32 len[2];
      gre_tunnel_key_t key[2];
      u8 matched[2];
      u32 tun_sw_if_index[2];

      if (PREDICT_TRUE (n_left_from >= 6))
	{
	  vlib_prefetch_buffer_data (b[2], LOAD);
	  vlib_prefetch_buffer_data (b[3], LOAD);
	  vlib_prefetch_buffer_header (b[4], STORE);
	  vlib_prefetch_buffer_header (b[5], STORE);
	}

      if (is_ipv6)
	{
	  /* ip6_local hands us the ip header, not the gre header */
	  ip6[0] = vlib_buffer_get_current (b[0]);
	  ip6[1] = vlib_buffer_get_current (b[1]);
	  gre[0] = (void *) (ip6[0] + 1);
	  gre[1] = (void *) (ip6[1] + 1);
	  // vlib_buffer_advance (b[0], sizeof (*ip6[0]) + sizeof (*gre[0]));
	  // vlib_buffer_advance (b[1], sizeof (*ip6[0]) + sizeof (*gre[0]));
	  //  Calculate total header size for each packet
	  u16 gre_hdr_size0 = sizeof (*gre[0]);
	  u16 gre_hdr_size1 = sizeof (*gre[1]);
	  if (gre[0]->flags_and_version & clib_host_to_net_u16 (GRE_FLAGS_KEY))
	    gre_hdr_size0 += sizeof (gre_key_t);
	  if (gre[1]->flags_and_version & clib_host_to_net_u16 (GRE_FLAGS_KEY))
	    gre_hdr_size1 += sizeof (gre_key_t);

	  // Single buffer advance for each packet
	  vlib_buffer_advance (b[0], sizeof (*ip6[0]) + gre_hdr_size0);
	  vlib_buffer_advance (b[1], sizeof (*ip6[1]) + gre_hdr_size1);
	}
      else
	{
	  /* ip4_local hands us the ip header, not the gre header */
	  ip4[0] = vlib_buffer_get_current (b[0]);
	  ip4[1] = vlib_buffer_get_current (b[1]);
	  gre[0] = (void *) (ip4[0] + 1);
	  // debug
	  clib_warning ("GRE header flags[0]: 0x%x",
			clib_net_to_host_u16 (gre[0]->flags_and_version));
	  gre[1] = (void *) (ip4[1] + 1);
	  // debug
	  clib_warning ("GRE header flags[1]: 0x%x",
			clib_net_to_host_u16 (gre[1]->flags_and_version));
	  // vlib_buffer_advance (b[0], sizeof (*ip4[0]) + sizeof (*gre[0]));
	  // vlib_buffer_advance (b[1], sizeof (*ip4[0]) + sizeof (*gre[0]));
	  //  Calculate total header size for each packet
	  u16 gre_hdr_size0 = sizeof (*gre[0]);
	  u16 gre_hdr_size1 = sizeof (*gre[1]);
	  if (gre[0]->flags_and_version & clib_host_to_net_u16 (GRE_FLAGS_KEY))
	    gre_hdr_size0 += sizeof (gre_key_t);
	  if (gre[1]->flags_and_version & clib_host_to_net_u16 (GRE_FLAGS_KEY))
	    gre_hdr_size1 += sizeof (gre_key_t);

	  // Single buffer advance for each packet
	  vlib_buffer_advance (b[0], sizeof (*ip4[0]) + gre_hdr_size0);
	  vlib_buffer_advance (b[1], sizeof (*ip4[1]) + gre_hdr_size1);
	}

      // GRE key processing here
      gre_key_t gre_key[2] = { 0, 0 };
      if (gre[0]->flags_and_version & clib_host_to_net_u16 (GRE_FLAGS_KEY))
	{
	  gre_header_with_key_t *grek = (gre_header_with_key_t *) gre[0];
	  gre_key[0] = clib_net_to_host_u32 (grek->key);
	  // debug
	  clib_warning ("Extracted GRE key[0]: %u", gre_key[0]);
	  // vlib_buffer_advance(b[0], sizeof(u32));
	  clib_warning ("Buffer[0] advanced by %u bytes",
			sizeof (gre_key_t)); // debug
	}
      if (gre[1]->flags_and_version & clib_host_to_net_u16 (GRE_FLAGS_KEY))
	{
	  gre_header_with_key_t *grek = (gre_header_with_key_t *) gre[1];
	  gre_key[1] = clib_net_to_host_u32 (grek->key);
	  // debug
	  clib_warning ("Extracted GRE key[1]: %u", gre_key[1]);
	  // vlib_buffer_advance(b[1], sizeof(u32));
	  clib_warning ("Buffer[1] advanced by %u bytes",
			sizeof (gre_key_t)); // debug
	}

      if (PREDICT_TRUE (cached_protocol == gre[0]->protocol))
	{
	  nidx[0] = cached_next_index;
	}
      else
	{
	  cached_next_index = nidx[0] =
	    sparse_vec_index (gm->next_by_protocol, gre[0]->protocol);
	  cached_protocol = gre[0]->protocol;
	}
      if (PREDICT_TRUE (cached_protocol == gre[1]->protocol))
	{
	  nidx[1] = cached_next_index;
	}
      else
	{
	  cached_next_index = nidx[1] =
	    sparse_vec_index (gm->next_by_protocol, gre[1]->protocol);
	  cached_protocol = gre[1]->protocol;
	}

      ni[0] = vec_elt (gm->next_by_protocol, nidx[0]);
      ni[1] = vec_elt (gm->next_by_protocol, nidx[1]);
      next[0] = ni[0].next_index;
      next[1] = ni[1].next_index;
      type[0] = ni[0].tunnel_type;
      type[1] = ni[1].tunnel_type;

      b[0]->error = nidx[0] == SPARSE_VEC_INVALID_INDEX ?
		      node->errors[GRE_ERROR_UNKNOWN_PROTOCOL] :
		      node->errors[GRE_ERROR_NONE];
      b[1]->error = nidx[1] == SPARSE_VEC_INVALID_INDEX ?
		      node->errors[GRE_ERROR_UNKNOWN_PROTOCOL] :
		      node->errors[GRE_ERROR_NONE];

      version[0] = clib_net_to_host_u16 (gre[0]->flags_and_version);
      version[1] = clib_net_to_host_u16 (gre[1]->flags_and_version);
      version[0] &= GRE_VERSION_MASK;
      version[1] &= GRE_VERSION_MASK;

      b[0]->error =
	version[0] ? node->errors[GRE_ERROR_UNSUPPORTED_VERSION] : b[0]->error;
      next[0] = version[0] ? GRE_INPUT_NEXT_DROP : next[0];
      b[1]->error =
	version[1] ? node->errors[GRE_ERROR_UNSUPPORTED_VERSION] : b[1]->error;
      next[1] = version[1] ? GRE_INPUT_NEXT_DROP : next[1];

      len[0] = vlib_buffer_length_in_chain (vm, b[0]);
      len[1] = vlib_buffer_length_in_chain (vm, b[1]);

      /* always search for P2P types in the DP */
      if (is_ipv6)
	{
	  gre_mk_key6 (&ip6[0]->dst_address, &ip6[0]->src_address,
		       vnet_buffer (b[0])->ip.fib_index, type[0],
		       TUNNEL_MODE_P2P, 0, gre_key[0], &key[0].gtk_v6);
	  gre_mk_key6 (&ip6[1]->dst_address, &ip6[1]->src_address,
		       vnet_buffer (b[1])->ip.fib_index, type[1],
		       TUNNEL_MODE_P2P, 0, gre_key[1], &key[1].gtk_v6);
	  matched[0] = gre_match_key6 (&cached_key.gtk_v6, &key[0].gtk_v6);
	  matched[1] = gre_match_key6 (&cached_key.gtk_v6, &key[1].gtk_v6);
	}
      else
	{
	  clib_warning ("Key before tunnel lookup[0]: %u",
			gre_key[0]); // debug
	  gre_mk_key4 (ip4[0]->dst_address, ip4[0]->src_address,
		       vnet_buffer (b[0])->ip.fib_index, type[0],
		       TUNNEL_MODE_P2P, 0, gre_key[0], &key[0].gtk_v4);
	  clib_warning ("Key before tunnel lookup[1]: %u",
			gre_key[1]); // debug
	  gre_mk_key4 (ip4[1]->dst_address, ip4[1]->src_address,
		       vnet_buffer (b[1])->ip.fib_index, type[1],
		       TUNNEL_MODE_P2P, 0, gre_key[1], &key[1].gtk_v4);
	  matched[0] = gre_match_key4 (&cached_key.gtk_v4, &key[0].gtk_v4);
	  matched[1] = gre_match_key4 (&cached_key.gtk_v4, &key[1].gtk_v4);
	}

      tun_sw_if_index[0] = cached_tun_sw_if_index;
      tun_sw_if_index[1] = cached_tun_sw_if_index;
      if (PREDICT_FALSE (!matched[0]))
	gre_tunnel_get (gm, node, b[0], &next[0], &key[0], &cached_key,
			&tun_sw_if_index[0], &cached_tun_sw_if_index, is_ipv6);
      if (PREDICT_FALSE (!matched[1]))
	gre_tunnel_get (gm, node, b[1], &next[1], &key[1], &cached_key,
			&tun_sw_if_index[1], &cached_tun_sw_if_index, is_ipv6);

      if (PREDICT_TRUE (next[0] > GRE_INPUT_NEXT_DROP))
	{
	  vlib_increment_combined_counter (
	    &gm->vnet_main->interface_main
	       .combined_sw_if_counters[VNET_INTERFACE_COUNTER_RX],
	    vm->thread_index, tun_sw_if_index[0], 1 /* packets */,
	    len[0] /* bytes */);
	  vnet_buffer (b[0])->sw_if_index[VLIB_RX] = tun_sw_if_index[0];
	}
      if (PREDICT_TRUE (next[1] > GRE_INPUT_NEXT_DROP))
	{
	  vlib_increment_combined_counter (
	    &gm->vnet_main->interface_main
	       .combined_sw_if_counters[VNET_INTERFACE_COUNTER_RX],
	    vm->thread_index, tun_sw_if_index[1], 1 /* packets */,
	    len[1] /* bytes */);
	  vnet_buffer (b[1])->sw_if_index[VLIB_RX] = tun_sw_if_index[1];
	}

      vnet_buffer (b[0])->sw_if_index[VLIB_TX] = (u32) ~0;
      vnet_buffer (b[1])->sw_if_index[VLIB_TX] = (u32) ~0;

      if (PREDICT_FALSE (b[0]->flags & VLIB_BUFFER_IS_TRACED))
	gre_trace (vm, node, b[0], tun_sw_if_index[0], ip6[0], ip4[0],
		   is_ipv6);
      if (PREDICT_FALSE (b[1]->flags & VLIB_BUFFER_IS_TRACED))
	gre_trace (vm, node, b[1], tun_sw_if_index[1], ip6[1], ip4[1],
		   is_ipv6);

      b += 2;
      next += 2;
      n_left_from -= 2;
    }

  while (n_left_from >= 1)
    {
      const ip6_header_t *ip6[1];
      const ip4_header_t *ip4[1];
      const gre_header_t *gre[1];
      u32 nidx[1];
      next_info_t ni[1];
      u8 type[1];
      u16 version[1];
      u32 len[1];
      gre_tunnel_key_t key[1];
      u8 matched[1];
      u32 tun_sw_if_index[1];

      if (PREDICT_TRUE (n_left_from >= 3))
	{
	  vlib_prefetch_buffer_data (b[1], LOAD);
	  vlib_prefetch_buffer_header (b[2], STORE);
	}

      if (is_ipv6)
	{
	  /* ip6_local hands us the ip header, not the gre header */
	  ip6[0] = vlib_buffer_get_current (b[0]);
	  gre[0] = (void *) (ip6[0] + 1);

	  // Calculate total header size
	  u16 gre_hdr_size = sizeof (*gre[0]);
	  if (gre[0]->flags_and_version & clib_host_to_net_u16 (GRE_FLAGS_KEY))
	    gre_hdr_size += sizeof (gre_key_t);
	  // Single buffer advance
	  vlib_buffer_advance (b[0], sizeof (*ip6[0]) + gre_hdr_size);
	}
      else
	{
	  /* ip4_local hands us the ip header, not the gre header */
	  ip4[0] = vlib_buffer_get_current (b[0]);
	  gre[0] = (void *) (ip4[0] + 1);
	  // debug
	  clib_warning ("GRE header flags: 0x%x",
			clib_net_to_host_u16 (gre[0]->flags_and_version));
	  // Calculate total header size
	  u16 gre_hdr_size = sizeof (*gre[0]);
	  if (gre[0]->flags_and_version & clib_host_to_net_u16 (GRE_FLAGS_KEY))
	    gre_hdr_size += sizeof (gre_key_t);
	  // Single buffer advance
	  vlib_buffer_advance (b[0], sizeof (*ip4[0]) + gre_hdr_size);
	}

      // GRE key processing here
      gre_key_t gre_key = 0;
      if (gre[0]->flags_and_version & clib_host_to_net_u16 (GRE_FLAGS_KEY))
	{
	  gre_header_with_key_t *grek = (gre_header_with_key_t *) gre[0];
	  gre_key = clib_net_to_host_u32 (grek->key);
	  // debug
	  clib_warning ("Extracted GRE key: %u", gre_key);
	  // vlib_buffer_advance(b[0], sizeof(u32));
	  // debug
	  clib_warning ("Buffer advanced by %u bytes", sizeof (gre_key_t));
	}

      if (PREDICT_TRUE (cached_protocol == gre[0]->protocol))
	{
	  nidx[0] = cached_next_index;
	}
      else
	{
	  cached_next_index = nidx[0] =
	    sparse_vec_index (gm->next_by_protocol, gre[0]->protocol);
	  cached_protocol = gre[0]->protocol;
	}

      ni[0] = vec_elt (gm->next_by_protocol, nidx[0]);
      next[0] = ni[0].next_index;
      type[0] = ni[0].tunnel_type;

      b[0]->error = nidx[0] == SPARSE_VEC_INVALID_INDEX ?
		      node->errors[GRE_ERROR_UNKNOWN_PROTOCOL] :
		      node->errors[GRE_ERROR_NONE];

      version[0] = clib_net_to_host_u16 (gre[0]->flags_and_version);
      version[0] &= GRE_VERSION_MASK;

      b[0]->error =
	version[0] ? node->errors[GRE_ERROR_UNSUPPORTED_VERSION] : b[0]->error;
      next[0] = version[0] ? GRE_INPUT_NEXT_DROP : next[0];

      len[0] = vlib_buffer_length_in_chain (vm, b[0]);

      if (is_ipv6)
	{
	  gre_mk_key6 (&ip6[0]->dst_address, &ip6[0]->src_address,
		       vnet_buffer (b[0])->ip.fib_index, type[0],
		       TUNNEL_MODE_P2P, 0, gre_key, &key[0].gtk_v6);
	  matched[0] = gre_match_key6 (&cached_key.gtk_v6, &key[0].gtk_v6);
	}
      else
	{
	  // debug
	  clib_warning ("Key before tunnel lookup: %u", gre_key);
	  gre_mk_key4 (ip4[0]->dst_address, ip4[0]->src_address,
		       vnet_buffer (b[0])->ip.fib_index, type[0],
		       TUNNEL_MODE_P2P, 0, gre_key, &key[0].gtk_v4);
	  matched[0] = gre_match_key4 (&cached_key.gtk_v4, &key[0].gtk_v4);
	  // Debug3
	  clib_warning (
	    "GRE tunnel lookup - src: %U dst: %U fib: %d type: %d key: %d",
	    format_ip4_address, &ip4[0]->src_address, format_ip4_address,
	    &ip4[0]->dst_address, vnet_buffer (b[0])->ip.fib_index, type[0],
	    gre_key);
	}

      tun_sw_if_index[0] = cached_tun_sw_if_index;
      if (PREDICT_FALSE (!matched[0]))
	gre_tunnel_get (gm, node, b[0], &next[0], &key[0], &cached_key,
			&tun_sw_if_index[0], &cached_tun_sw_if_index, is_ipv6);

      if (PREDICT_TRUE (next[0] > GRE_INPUT_NEXT_DROP))
	{
	  vlib_increment_combined_counter (
	    &gm->vnet_main->interface_main
	       .combined_sw_if_counters[VNET_INTERFACE_COUNTER_RX],
	    vm->thread_index, tun_sw_if_index[0], 1 /* packets */,
	    len[0] /* bytes */);
	  vnet_buffer (b[0])->sw_if_index[VLIB_RX] = tun_sw_if_index[0];
	}

      vnet_buffer (b[0])->sw_if_index[VLIB_TX] = (u32) ~0;

      if (PREDICT_FALSE (b[0]->flags & VLIB_BUFFER_IS_TRACED))
	gre_trace (vm, node, b[0], tun_sw_if_index[0], ip6[0], ip4[0],
		   is_ipv6);

      b += 1;
      next += 1;
      n_left_from -= 1;
    }

  vlib_buffer_enqueue_to_next (vm, node, from, nexts, frame->n_vectors);

  vlib_node_increment_counter (
    vm, is_ipv6 ? gre6_input_node.index : gre4_input_node.index,
    GRE_ERROR_PKTS_DECAP, n_left_from);

  return frame->n_vectors;
}

VLIB_NODE_FN (gre4_input_node)
(vlib_main_t *vm, vlib_node_runtime_t *node, vlib_frame_t *from_frame)
{
  return gre_input (vm, node, from_frame, /* is_ip6 */ 0);
}

VLIB_NODE_FN (gre6_input_node)
(vlib_main_t *vm, vlib_node_runtime_t *node, vlib_frame_t *from_frame)
{
  return gre_input (vm, node, from_frame, /* is_ip6 */ 1);
}

static char *gre_error_strings[] = {
#define gre_error(n, s) s,
#include "error.def"
#undef gre_error
};

VLIB_REGISTER_NODE (gre4_input_node) = {
   .name = "gre4-input",
   /* Takes a vector of packets. */
   .vector_size = sizeof (u32),
 
   .n_errors = GRE_N_ERROR,
   .error_strings = gre_error_strings,
 
   .n_next_nodes = GRE_INPUT_N_NEXT,
   .next_nodes = {
#define _(s, n) [GRE_INPUT_NEXT_##s] = n,
     foreach_gre_input_next
#undef _
   },
 
   .format_buffer = format_gre_header_with_length,
   .format_trace = format_gre_rx_trace,
   .unformat_buffer = unformat_gre_header,
 };

VLIB_REGISTER_NODE (gre6_input_node) = {
   .name = "gre6-input",
   /* Takes a vector of packets. */
   .vector_size = sizeof (u32),
 
   .runtime_data_bytes = sizeof (gre_input_runtime_t),
 
   .n_errors = GRE_N_ERROR,
   .error_strings = gre_error_strings,
 
   .n_next_nodes = GRE_INPUT_N_NEXT,
   .next_nodes = {
#define _(s, n) [GRE_INPUT_NEXT_##s] = n,
     foreach_gre_input_next
#undef _
   },
 
   .format_buffer = format_gre_header_with_length,
   .format_trace = format_gre_rx_trace,
   .unformat_buffer = unformat_gre_header,
 };

#ifndef CLIB_MARCH_VARIANT
void
gre_register_input_protocol (vlib_main_t *vm, gre_protocol_t protocol,
			     u32 node_index, gre_tunnel_type_t tunnel_type)
{
  gre_main_t *em = &gre_main;
  gre_protocol_info_t *pi;
  next_info_t *n;
  u32 i;

  {
    clib_error_t *error = vlib_call_init_function (vm, gre_input_init);
    if (error)
      clib_error_report (error);
  }

  pi = gre_get_protocol_info (em, protocol);
  pi->node_index = node_index;
  pi->tunnel_type = tunnel_type;
  pi->next_index = vlib_node_add_next (vm, gre4_input_node.index, node_index);
  i = vlib_node_add_next (vm, gre6_input_node.index, node_index);
  ASSERT (i == pi->next_index);

  /* Setup gre protocol -> next index sparse vector mapping. */
  n = sparse_vec_validate (em->next_by_protocol,
			   clib_host_to_net_u16 (protocol));
  n->next_index = pi->next_index;
  n->tunnel_type = tunnel_type;
}

static void
gre_setup_node (vlib_main_t *vm, u32 node_index)
{
  vlib_node_t *n = vlib_get_node (vm, node_index);
  pg_node_t *pn = pg_get_node (node_index);

  n->format_buffer = format_gre_header_with_length;
  n->unformat_buffer = unformat_gre_header;
  pn->unformat_edit = unformat_pg_gre_header;
}

static clib_error_t *
gre_input_init (vlib_main_t *vm)
{
  gre_main_t *gm = &gre_main;
  vlib_node_t *ethernet_input, *ip4_input, *ip6_input, *mpls_unicast_input;

  {
    clib_error_t *error;
    error = vlib_call_init_function (vm, gre_init);
    if (error)
      clib_error_report (error);
  }

  gre_setup_node (vm, gre4_input_node.index);
  gre_setup_node (vm, gre6_input_node.index);

  gm->next_by_protocol =
    sparse_vec_new (/* elt bytes */ sizeof (gm->next_by_protocol[0]),
		    /* bits in index */ BITS (((gre_header_t *) 0)->protocol));

  /* These could be moved to the supported protocol input node defn's */
  ethernet_input = vlib_get_node_by_name (vm, (u8 *) "ethernet-input");
  ASSERT (ethernet_input);
  ip4_input = vlib_get_node_by_name (vm, (u8 *) "ip4-input");
  ASSERT (ip4_input);
  ip6_input = vlib_get_node_by_name (vm, (u8 *) "ip6-input");
  ASSERT (ip6_input);
  mpls_unicast_input = vlib_get_node_by_name (vm, (u8 *) "mpls-input");
  ASSERT (mpls_unicast_input);

  gre_register_input_protocol (vm, GRE_PROTOCOL_teb, ethernet_input->index,
			       GRE_TUNNEL_TYPE_TEB);

  gre_register_input_protocol (vm, GRE_PROTOCOL_ip4, ip4_input->index,
			       GRE_TUNNEL_TYPE_L3);

  gre_register_input_protocol (vm, GRE_PROTOCOL_ip6, ip6_input->index,
			       GRE_TUNNEL_TYPE_L3);

  gre_register_input_protocol (vm, GRE_PROTOCOL_mpls_unicast,
			       mpls_unicast_input->index, GRE_TUNNEL_TYPE_L3);

  return 0;
}

VLIB_INIT_FUNCTION (gre_input_init);

#endif /* CLIB_MARCH_VARIANT */
/*
 * fd.io coding-style-patch-verification: ON
 *
 * Local Variables:
 * eval: (c-set-style "gnu")
 * End:
 */
