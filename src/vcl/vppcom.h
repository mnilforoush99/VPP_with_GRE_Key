/*
 * Copyright (c) 2017-2020 Cisco and/or its affiliates.
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

#ifndef included_vppcom_h
#define included_vppcom_h

#ifdef __FreeBSD__
#include <sys/types.h>
#endif /* __FreeBSD__ */
#include <netdb.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/epoll.h>

/* clang-format off */

#ifdef __cplusplus
extern "C"
{
#endif

/*
 * VPPCOM Public API Definitions, Enums, and Data Structures
 */
#define INVALID_SESSION_ID                  	((u32)~0)
#define VPPCOM_CONF_DEFAULT                  	"/etc/vpp/vcl.conf"
#define VPPCOM_ENV_CONF                      	"VCL_CONFIG"
#define VPPCOM_ENV_DEBUG                     	"VCL_DEBUG"
#define VPPCOM_ENV_APP_PROXY_TRANSPORT_TCP   	"VCL_APP_PROXY_TRANSPORT_TCP"
#define VPPCOM_ENV_APP_PROXY_TRANSPORT_UDP   	"VCL_APP_PROXY_TRANSPORT_UDP"
#define VPPCOM_ENV_APP_NAMESPACE_ID          	"VCL_APP_NAMESPACE_ID"
#define VPPCOM_ENV_APP_NAMESPACE_SECRET      	"VCL_APP_NAMESPACE_SECRET"
#define VPPCOM_ENV_APP_SCOPE_LOCAL           	"VCL_APP_SCOPE_LOCAL"
#define VPPCOM_ENV_APP_SCOPE_GLOBAL          	"VCL_APP_SCOPE_GLOBAL"
#define VPPCOM_ENV_APP_USE_MQ_EVENTFD		"VCL_APP_USE_MQ_EVENTFD"
#define VPPCOM_ENV_VPP_API_SOCKET           	"VCL_VPP_API_SOCKET"
#define VPPCOM_ENV_VPP_SAPI_SOCKET		"VCL_VPP_SAPI_SOCKET"

typedef enum vppcom_proto_
{
  VPPCOM_PROTO_TCP = 0,
  VPPCOM_PROTO_UDP,
  VPPCOM_PROTO_NONE,
  VPPCOM_PROTO_TLS,
  VPPCOM_PROTO_QUIC,
  VPPCOM_PROTO_DTLS,
  VPPCOM_PROTO_SRTP,
  VPPCOM_PROTO_HTTP,
} vppcom_proto_t;

typedef enum
{
  VPPCOM_IS_IP6 = 0,
  VPPCOM_IS_IP4,
} vppcom_is_ip4_t;

typedef struct vppcom_endpt_tlv_t_
{
  uint32_t data_type;
  uint32_t data_len;
  uint8_t data[0];
} vppcom_endpt_tlv_t;

typedef struct vppcom_endpt_t_
{
  uint8_t unused;		/**< unused */
  uint8_t is_ip4;		/**< flag set if if ip is ipv4 */
  uint8_t *ip;			/**< pointer to ip address */
  uint16_t port;		/**< transport port */
  uint64_t unused2;		/**< unused */
  uint32_t app_tlv_len;		/**< length of app provided tlvs */
  vppcom_endpt_tlv_t *app_tlvs;	/**< array of app provided tlvs */
} vppcom_endpt_t;

#define VCL_UDP_OPTS_BASE (VPPCOM_PROTO_UDP << 16)
#define VCL_UDP_SEGMENT	  (VCL_UDP_OPTS_BASE + 0)

/* By convention we'll use 127 for IP since we don't support IP as protocol */
#define VCL_IP_OPTS_BASE (127 << 16)
#define VCL_IP_PKTINFO	 (VCL_IP_OPTS_BASE + 1)

#define VCL_EP_APP_TLV_LEN(tlv_) (sizeof (vppcom_endpt_tlv_t) + tlv->data_len)
#define VCL_EP_APP_TLV_POS(ep_, tlv_) ((void *)ep_->app_tlvs - (void *)tlv_)
#define VCL_EP_APP_TLV_LEN_LEFT(ep_, tlv_)                                    \
  (ep_->app_tlv_len - VCL_EP_APP_TLV_POS (ep_, tlv_))
#define VCL_EP_NEXT_APP_TLV(ep_, tlv_)                                        \
  (VCL_EP_APP_TLV_LEN (tlv_) < VCL_EP_APP_TLV_POS (ep_, tlv_) ? (             \
       (vppcom_endpt_tlv_t *)((uint8_t *)tlv_ + VCL_EP_APP_TLV_LEN (tlv_)))   \
                                                              : 0)

typedef uint32_t vcl_session_handle_t;

typedef struct vppcom_cert_key_pair_
{
  char *cert;
  char *key;
  uint32_t cert_len;
  uint32_t key_len;
} vppcom_cert_key_pair_t;

typedef enum
{
  VPPCOM_OK = 0,
  VPPCOM_EAGAIN = -EAGAIN,
  VPPCOM_EWOULDBLOCK = -EWOULDBLOCK,
  VPPCOM_EINPROGRESS = -EINPROGRESS,
  VPPCOM_EFAULT = -EFAULT,
  VPPCOM_ENOMEM = -ENOMEM,
  VPPCOM_EINVAL = -EINVAL,
#ifdef __linux__
  VPPCOM_EBADFD = -EBADFD,
#else
  VPPCOM_EBADFD = -EBADF,
#endif /* __linux__ */
  VPPCOM_EAFNOSUPPORT = -EAFNOSUPPORT,
  VPPCOM_ECONNABORTED = -ECONNABORTED,
  VPPCOM_ECONNRESET = -ECONNRESET,
  VPPCOM_ENOTCONN = -ENOTCONN,
  VPPCOM_ECONNREFUSED = -ECONNREFUSED,
  VPPCOM_ETIMEDOUT = -ETIMEDOUT,
  VPPCOM_EEXIST = -EEXIST,
  VPPCOM_ENOPROTOOPT = -ENOPROTOOPT,
  VPPCOM_EPIPE = -EPIPE,
  VPPCOM_ENOENT = -ENOENT,
  VPPCOM_EADDRINUSE = -EADDRINUSE,
  VPPCOM_ENOTSUP = -ENOTSUP
} vppcom_error_t;

typedef enum
{
  VPPCOM_ATTR_GET_NREAD,
  VPPCOM_ATTR_GET_NWRITE,
  VPPCOM_ATTR_GET_FLAGS,
  VPPCOM_ATTR_SET_FLAGS,
  VPPCOM_ATTR_GET_LCL_ADDR,
  VPPCOM_ATTR_SET_LCL_ADDR,
  VPPCOM_ATTR_GET_PEER_ADDR,
  VPPCOM_ATTR_GET_UNUSED,
  VPPCOM_ATTR_SET_UNUSED,
  VPPCOM_ATTR_GET_PROTOCOL,
  VPPCOM_ATTR_GET_LISTEN,
  VPPCOM_ATTR_GET_ERROR,
  VPPCOM_ATTR_GET_TX_FIFO_LEN,
  VPPCOM_ATTR_SET_TX_FIFO_LEN,
  VPPCOM_ATTR_GET_RX_FIFO_LEN,
  VPPCOM_ATTR_SET_RX_FIFO_LEN,
  VPPCOM_ATTR_GET_REUSEADDR,
  VPPCOM_ATTR_SET_REUSEADDR,
  VPPCOM_ATTR_GET_REUSEPORT,
  VPPCOM_ATTR_SET_REUSEPORT,
  VPPCOM_ATTR_GET_BROADCAST,
  VPPCOM_ATTR_SET_BROADCAST,
  VPPCOM_ATTR_GET_V6ONLY,
  VPPCOM_ATTR_SET_V6ONLY,
  VPPCOM_ATTR_GET_KEEPALIVE,
  VPPCOM_ATTR_SET_KEEPALIVE,
  VPPCOM_ATTR_GET_TCP_NODELAY,
  VPPCOM_ATTR_SET_TCP_NODELAY,
  VPPCOM_ATTR_GET_TCP_KEEPIDLE,
  VPPCOM_ATTR_SET_TCP_KEEPIDLE,
  VPPCOM_ATTR_GET_TCP_KEEPINTVL,
  VPPCOM_ATTR_SET_TCP_KEEPINTVL,
  VPPCOM_ATTR_GET_TCP_USER_MSS,
  VPPCOM_ATTR_SET_TCP_USER_MSS,
  VPPCOM_ATTR_SET_CONNECTED,
  VPPCOM_ATTR_SET_CKPAIR,
  VPPCOM_ATTR_SET_VRF,
  VPPCOM_ATTR_GET_VRF,
  VPPCOM_ATTR_GET_DOMAIN,
  VPPCOM_ATTR_SET_ENDPT_EXT_CFG,
  VPPCOM_ATTR_SET_DSCP,
  VPPCOM_ATTR_SET_IP_PKTINFO,
  VPPCOM_ATTR_GET_IP_PKTINFO,
  VPPCOM_ATTR_GET_ORIGINAL_DST,
  VPPCOM_ATTR_GET_NWRITEQ,
  VPPCOM_ATTR_GET_EXT_ENDPT,
} vppcom_attr_op_t;

typedef struct _vcl_poll
{
  uint32_t fds_ndx;
  vcl_session_handle_t sh;
  short events;
  short revents;
} vcl_poll_t;

typedef struct vppcom_data_segment_
{
  unsigned char *data;
  uint32_t len;
} vppcom_data_segment_t;

typedef vppcom_data_segment_t vppcom_data_segments_t[2];

typedef unsigned long vcl_si_set;

/*
 * VPPCOM Public API Functions
 */

extern int vppcom_app_create (const char *app_name);
extern void vppcom_app_destroy (void);

extern int vppcom_session_create (uint8_t proto, uint8_t is_nonblocking);
extern int vppcom_session_shutdown (uint32_t session_handle, int how);
extern int vppcom_session_close (uint32_t session_handle);
extern int vppcom_session_bind (uint32_t session_handle, vppcom_endpt_t * ep);
extern int vppcom_session_listen (uint32_t session_handle, uint32_t q_len);

extern int vppcom_session_accept (uint32_t session_handle,
				  vppcom_endpt_t * client_ep, uint32_t flags);

extern int vppcom_session_connect (uint32_t session_handle,
				   vppcom_endpt_t * server_ep);
extern int vppcom_session_stream_connect (uint32_t session_handle,
					  uint32_t parent_session_handle);
extern int vppcom_session_read (uint32_t session_handle, void *buf, size_t n);
extern int vppcom_session_write (uint32_t session_handle, void *buf,
				 size_t n);
extern int vppcom_session_write_msg (uint32_t session_handle, void *buf,
				     size_t n);

extern int vppcom_select (int n_bits, vcl_si_set * read_map,
			  vcl_si_set * write_map, vcl_si_set * except_map,
			  double wait_for_time);

extern int vppcom_epoll_create (void);
extern int vppcom_epoll_ctl (uint32_t vep_handle, int op,
			     uint32_t session_handle,
			     struct epoll_event *event);
extern int vppcom_epoll_wait (uint32_t vep_handle, struct epoll_event *events,
			      int maxevents, double wait_for_time);
extern int vppcom_session_attr (uint32_t session_handle, uint32_t op,
				void *buffer, uint32_t * buflen);
extern int vppcom_session_recvfrom (uint32_t session_handle, void *buffer,
				    uint32_t buflen, int flags,
				    vppcom_endpt_t * ep);
extern int vppcom_session_sendto (uint32_t session_handle, void *buffer,
				  uint32_t buflen, int flags,
				  vppcom_endpt_t * ep);
extern int vppcom_poll (vcl_poll_t * vp, uint32_t n_sids,
			double wait_for_time);
extern int vppcom_mq_epoll_fd (void);
extern int vppcom_session_index (vcl_session_handle_t session_handle);
extern int vppcom_session_worker (vcl_session_handle_t session_handle);

extern int vppcom_session_read_segments (uint32_t session_handle,
					 vppcom_data_segment_t * ds,
					 uint32_t n_segments,
					 uint32_t max_bytes);
extern int vppcom_session_write_segments (uint32_t session_handle,
					 vppcom_data_segment_t * ds,
					 uint32_t n_segments);
extern void vppcom_session_free_segments (uint32_t session_handle,
					  uint32_t n_bytes);
extern int vppcom_add_cert_key_pair (vppcom_cert_key_pair_t *ckpair);
extern int vppcom_del_cert_key_pair (uint32_t ckpair_index);
extern int vppcom_unformat_proto (uint8_t * proto, char *proto_str);
extern int vppcom_session_is_connectable_listener (uint32_t session_handle);
extern int vppcom_session_listener (uint32_t session_handle);
extern int vppcom_session_n_accepted (uint32_t session_handle);

extern const char *vppcom_proto_str (vppcom_proto_t proto);
extern const char *vppcom_retval_str (int retval);

/**
 * Request from application to register a new worker
 *
 * Expectation is that applications will make use of this after a new pthread
 * is spawned.
 */
extern int vppcom_worker_register (void);

/**
 * Unregister current worker
 */
extern void vppcom_worker_unregister (void);

/**
 * Retrieve current worker index
 */
extern int vppcom_worker_index (void);

/**
 * Set current worker index
 */
extern void vppcom_worker_index_set (int);

/**
 * Returns the current worker's message queues epoll fd
 *
 * This only works if vcl is configured to do eventfd based message queue
 * notifications.
 */
extern int vppcom_worker_mqs_epfd (void);

/**
 * Returns Session error
 *
 * Application can use this API to find the detailed session error
 */
extern int vppcom_session_get_error (uint32_t session_handle);

/**
 * Returns true if current worker is disconnected from vpp
 *
 * Application can use this API to check if VPP is disconnected
 * as long as `use-mq-eventfd` is being set
 */
extern int vppcom_worker_is_detached (void);

#ifdef __cplusplus
}
#endif
/* clang-format on */

#endif /* included_vppcom_h */

/*
 * fd.io coding-style-patch-verification: ON
 *
 * Local Variables:
 * eval: (c-set-style "gnu")
 * End:
 */
