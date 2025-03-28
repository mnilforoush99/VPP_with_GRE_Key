#!/usr/bin/env python3
""" BFD tests """

from __future__ import division

import binascii
from collections import namedtuple
import hashlib
import ipaddress
import reprlib
import time
import unittest
from random import randint, shuffle, getrandbits
from socket import AF_INET, AF_INET6
from parameterized import parameterized, parameterized_class

import scapy.compat
from scapy.layers.inet import UDP, IP
from scapy.layers.inet6 import IPv6
from scapy.layers.l2 import Ether, GRE
from scapy.packet import Raw

from config import config
from bfd import (
    VppBFDAuthKey,
    BFD,
    BFDAuthType,
    VppBFDUDPSession,
    BFDDiagCode,
    BFDState,
    BFD_vpp_echo,
    BFD_UDP_SH_PORT,
    BFD_UDP_MH_PORT,
)
from framework import VppTestCase
from asfframework import (
    tag_fixme_vpp_workers,
    tag_fixme_debian11,
    tag_run_solo,
    VppTestRunner,
)
from util import ppp
from vpp_ip import DpoProto
from vpp_ip_route import VppIpRoute, VppRoutePath
from vpp_lo_interface import VppLoInterface
from vpp_papi_provider import UnexpectedApiReturnValueError, CliFailedCommandError
from vpp_pg_interface import CaptureTimeoutError, is_ipv6_misc
from vpp_gre_interface import VppGreInterface

USEC_IN_SEC = 1000000
BFD_STATS_V4_SH_PATH = "/bfd/udp4/sessions"
BFD_STATS_V6_SH_PATH = "/bfd/udp6/sessions"
BFD_STATS_V4_MH_PATH = "/bfd/udp4/sessions_mh"
BFD_STATS_V6_MH_PATH = "/bfd/udp6/sessions_mh"
BFD_IPV4_REMOTE_ADDR = "2.2.2.2"
BFD_IPV6_REMOTE_ADDR = "2::2"
BFD_IPV4_REMOTE_ADDR2 = "3.3.3.3"
BFD_IPV6_REMOTE_ADDR2 = "3::3"


def set_ipv4_pfx_route_info(cls, pg_if, dst_ip, set_src):
    try:
        if cls.multihop:
            paths = []
            # Get list of all the next hops
            for nh_host in pg_if.remote_hosts:
                nh_host_ip = nh_host.ip4
                paths.append(VppRoutePath(nh_host_ip, pg_if.sw_if_index))
            cls.dst_ip_net = dst_ip
            # Create a route pointing to list of next hops
            if paths:
                rip = VppIpRoute(cls, cls.dst_ip_net, 32, paths)
                rip.add_vpp_config()
                cls.logger.info("Route via %s on %s created" % (paths, pg_if.name))
            if set_src:
                cls.src_ip_net = cls.loopback0.local_ip4
        else:
            cls.dst_ip_net = pg_if.remote_ip4
            if set_src:
                cls.src_ip_net = pg_if.local_ip4
    except BaseException:
        cls.vapi.want_bfd_events(enable_disable=0)
        raise


def set_ipv6_pfx_route_info(cls, pg_if, dst_ip, set_src):
    try:
        if cls.multihop:
            paths = []
            # Get list of all the next hops
            for nh_host in pg_if.remote_hosts:
                nh_host_ip = nh_host.ip6
                paths.append(VppRoutePath(nh_host_ip, pg_if.sw_if_index))
            cls.dst_ip6_net = dst_ip
            # Create a route pointing to list of next hops
            if paths:
                rip = VppIpRoute(cls, cls.dst_ip6_net, 128, paths)
                rip.add_vpp_config()
                cls.logger.info("Route via %s on %s created" % (paths, pg_if.name))
            if set_src:
                cls.src_ip6_net = cls.loopback0.local_ip6
        else:
            cls.dst_ip6_net = cls.pg0.remote_ip6
            if set_src:
                cls.src_ip6_net = cls.pg0.local_ip6
    except BaseException:
        cls.vapi.want_bfd_events(enable_disable=0)
        raise


class AuthKeyFactory(object):
    """Factory class for creating auth keys with unique conf key ID"""

    def __init__(self):
        self._conf_key_ids = {}

    def create_random_key(self, test, auth_type=BFDAuthType.keyed_sha1):
        """create a random key with unique conf key id"""
        conf_key_id = randint(0, 0xFFFFFFFF)
        while conf_key_id in self._conf_key_ids:
            conf_key_id = randint(0, 0xFFFFFFFF)
        self._conf_key_ids[conf_key_id] = 1
        key = scapy.compat.raw(
            bytearray([randint(0, 255) for _ in range(randint(1, 20))])
        )
        return VppBFDAuthKey(
            test=test, auth_type=auth_type, conf_key_id=conf_key_id, key=key
        )


@parameterized_class(
    [
        {"multihop": False},
        {"multihop": True},
    ]
)
class BFDAPITestCase(VppTestCase):

    pg0 = None
    pg1 = None
    interface0 = None
    interface1 = None
    src_ip_net = ""
    dst_ip_net = ""
    src_ip_net2 = ""
    dst_ip_net2 = ""
    src_ip6_net = ""
    dst_ip6_net = ""
    src_ip6_net2 = ""
    dst_ip6_net2 = ""

    @classmethod
    def setUpClass(cls):
        super(BFDAPITestCase, cls).setUpClass()
        hoptype_str = "MultiHop" if cls.multihop else "SingleHop"
        cls.__doc__ = (
            f"""""Bidirectional Forwarding Detection (BFD) - API, {hoptype_str}"""
        )
        cls.vapi.cli("set log class bfd level debug")
        try:
            cls.create_pg_interfaces(range(2))
            cls.create_loopback_interfaces(2)
            cls.loopback0 = cls.lo_interfaces[0]
            cls.loopback0.config_ip4()
            cls.loopback0.admin_up()
            cls.loopback1 = cls.lo_interfaces[1]
            cls.loopback1.config_ip4()
            cls.loopback1.admin_up()
            for i in cls.pg_interfaces:
                i.config_ip4()
                i.config_ip6()
                i.resolve_arp()

        except Exception:
            super(BFDAPITestCase, cls).tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        super(BFDAPITestCase, cls).tearDownClass()

    def setUp(self):
        super(BFDAPITestCase, self).setUp()
        self.factory = AuthKeyFactory()

        if self.multihop:
            self.interface0 = None
            self.interface1 = None
            self.src_ip_net = self.loopback0.local_ip4
            self.src_ip6_net = self.loopback0.local_ip6
            self.dst_ip_net = BFD_IPV4_REMOTE_ADDR
            self.dst_ip6_net = BFD_IPV6_REMOTE_ADDR
            self.src_ip_net2 = self.loopback1.local_ip4
            self.src_ip6_net2 = self.loopback1.local_ip6
            self.dst_ip_net2 = BFD_IPV4_REMOTE_ADDR2
            self.dst_ip6_net2 = BFD_IPV6_REMOTE_ADDR2
        else:
            self.interface0 = self.pg0
            self.interface1 = self.pg1
            self.src_ip_net = self.pg0.local_ip4
            self.src_ip6_net = self.pg0.local_ip6
            self.dst_ip_net = self.pg0.remote_ip4
            self.dst_ip6_net = self.pg0.remote_ip6
            self.src_ip_net2 = self.pg1.local_ip4
            self.src_ip6_net2 = self.pg1.local_ip6
            self.dst_ip_net2 = BFD_IPV4_REMOTE_ADDR2
            self.dst_ip6_net2 = BFD_IPV6_REMOTE_ADDR2

    def test_add_bfd(self):
        """create a BFD session"""
        session = VppBFDUDPSession(
            self,
            self.interface0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
        )
        session.add_vpp_config()
        self.logger.debug("Session state is %s", session.state)
        session.remove_vpp_config()
        session.add_vpp_config()
        self.logger.debug("Session state is %s", session.state)
        session.remove_vpp_config()

    def test_double_add(self):
        """create the same BFD session twice (negative case)"""
        session = VppBFDUDPSession(
            self,
            self.interface0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
        )
        session.add_vpp_config()

        with self.vapi.assert_negative_api_retval():
            session.add_vpp_config()

        session.remove_vpp_config()

    def test_add_bfd6(self):
        """create IPv6 BFD session"""
        session = VppBFDUDPSession(
            self,
            self.interface0,
            self.dst_ip6_net,
            local_addr=self.src_ip6_net,
            af=AF_INET6,
            multihop=self.multihop,
        )
        session.add_vpp_config()
        self.logger.debug("Session state is %s", session.state)
        session.remove_vpp_config()
        session.add_vpp_config()
        self.logger.debug("Session state is %s", session.state)
        session.remove_vpp_config()

    def test_mod_bfd(self):
        """modify BFD session parameters"""
        session = VppBFDUDPSession(
            self,
            self.interface0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            desired_min_tx=50000,
            required_min_rx=10000,
            detect_mult=1,
        )
        session.add_vpp_config()
        s = session.get_bfd_udp_session_dump_entry()
        self.assert_equal(
            session.desired_min_tx, s.desired_min_tx, "desired min transmit interval"
        )
        self.assert_equal(
            session.required_min_rx, s.required_min_rx, "required min receive interval"
        )
        self.assert_equal(session.detect_mult, s.detect_mult, "detect mult")
        session.modify_parameters(
            desired_min_tx=session.desired_min_tx * 2,
            required_min_rx=session.required_min_rx * 2,
            detect_mult=session.detect_mult * 2,
        )
        s = session.get_bfd_udp_session_dump_entry()
        self.assert_equal(
            session.desired_min_tx, s.desired_min_tx, "desired min transmit interval"
        )
        self.assert_equal(
            session.required_min_rx, s.required_min_rx, "required min receive interval"
        )
        self.assert_equal(session.detect_mult, s.detect_mult, "detect mult")

    def test_upd_bfd(self):
        """Create/Modify w/ Update BFD session parameters"""
        session = VppBFDUDPSession(
            self,
            self.interface0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            desired_min_tx=50000,
            required_min_rx=10000,
            detect_mult=1,
        )
        session.upd_vpp_config()
        s = session.get_bfd_udp_session_dump_entry()
        self.assert_equal(
            session.desired_min_tx, s.desired_min_tx, "desired min transmit interval"
        )
        self.assert_equal(
            session.required_min_rx, s.required_min_rx, "required min receive interval"
        )

        self.assert_equal(session.detect_mult, s.detect_mult, "detect mult")
        session.upd_vpp_config(
            desired_min_tx=session.desired_min_tx * 2,
            required_min_rx=session.required_min_rx * 2,
            detect_mult=session.detect_mult * 2,
        )
        s = session.get_bfd_udp_session_dump_entry()
        self.assert_equal(
            session.desired_min_tx, s.desired_min_tx, "desired min transmit interval"
        )
        self.assert_equal(
            session.required_min_rx, s.required_min_rx, "required min receive interval"
        )
        self.assert_equal(session.detect_mult, s.detect_mult, "detect mult")

    def test_add_sha1_keys(self):
        """add SHA1 keys"""
        key_count = 10
        keys = [self.factory.create_random_key(self) for i in range(0, key_count)]
        for key in keys:
            self.assertFalse(key.query_vpp_config())
        for key in keys:
            key.add_vpp_config()
        for key in keys:
            self.assertTrue(key.query_vpp_config())
        # remove randomly
        indexes = list(range(key_count))
        shuffle(indexes)
        removed = []
        for i in indexes:
            key = keys[i]
            key.remove_vpp_config()
            removed.append(i)
            for j in range(key_count):
                key = keys[j]
                if j in removed:
                    self.assertFalse(key.query_vpp_config())
                else:
                    self.assertTrue(key.query_vpp_config())
        # should be removed now
        for key in keys:
            self.assertFalse(key.query_vpp_config())
        # add back and remove again
        for key in keys:
            key.add_vpp_config()
        for key in keys:
            self.assertTrue(key.query_vpp_config())
        for key in keys:
            key.remove_vpp_config()
        for key in keys:
            self.assertFalse(key.query_vpp_config())

    def test_add_bfd_sha1(self):
        """create a BFD session (SHA1)"""
        key = self.factory.create_random_key(self)
        key.add_vpp_config()
        session = VppBFDUDPSession(
            self,
            self.interface0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
        )
        session.add_vpp_config()
        self.logger.debug("Session state is %s", session.state)
        session.remove_vpp_config()
        session.add_vpp_config()
        self.logger.debug("Session state is %s", session.state)
        session.remove_vpp_config()

    def test_double_add_sha1(self):
        """create the same BFD session twice (negative case) (SHA1)"""
        key = self.factory.create_random_key(self)
        key.add_vpp_config()
        session = VppBFDUDPSession(
            self,
            self.interface0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
        )
        session.add_vpp_config()
        with self.assertRaises(Exception):
            session.add_vpp_config()

    def test_add_auth_nonexistent_key(self):
        """create BFD session using non-existent SHA1 (negative case)"""
        session = VppBFDUDPSession(
            self,
            self.interface0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=self.factory.create_random_key(self),
        )
        self.session = VppBFDUDPSession(
            self,
            self.interface0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
        )
        with self.assertRaises(Exception):
            session.add_vpp_config()

    def test_shared_sha1_key(self):
        """single SHA1 key shared by multiple BFD sessions"""
        key = self.factory.create_random_key(self)
        key.add_vpp_config()
        sessions = [
            VppBFDUDPSession(
                self,
                self.interface0,
                self.dst_ip_net,
                local_addr=self.src_ip_net,
                multihop=self.multihop,
                sha1_key=key,
            ),
            VppBFDUDPSession(
                self,
                self.interface0,
                self.dst_ip6_net,
                local_addr=self.src_ip6_net,
                af=AF_INET6,
                multihop=self.multihop,
                sha1_key=key,
            ),
            VppBFDUDPSession(
                self,
                self.interface1,
                self.dst_ip_net2,
                local_addr=self.src_ip_net2,
                multihop=self.multihop,
                sha1_key=key,
            ),
            VppBFDUDPSession(
                self,
                self.interface1,
                self.dst_ip6_net2,
                local_addr=self.src_ip6_net2,
                af=AF_INET6,
                multihop=self.multihop,
                sha1_key=key,
            ),
        ]
        for s in sessions:
            s.add_vpp_config()
        removed = 0
        for s in sessions:
            e = key.get_bfd_auth_keys_dump_entry()
            self.assert_equal(
                e.use_count, len(sessions) - removed, "Use count for shared key"
            )
            s.remove_vpp_config()
            removed += 1
        e = key.get_bfd_auth_keys_dump_entry()
        self.assert_equal(
            e.use_count, len(sessions) - removed, "Use count for shared key"
        )

    def test_activate_auth(self):
        """activate SHA1 authentication"""
        key = self.factory.create_random_key(self)
        key.add_vpp_config()
        session = VppBFDUDPSession(
            self,
            self.interface0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
        )
        session.add_vpp_config()
        session.activate_auth(key)

    def test_deactivate_auth(self):
        """deactivate SHA1 authentication"""
        key = self.factory.create_random_key(self)
        key.add_vpp_config()
        session = VppBFDUDPSession(
            self,
            self.interface0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
        )
        session.add_vpp_config()
        session.activate_auth(key)
        session.deactivate_auth()

    def test_change_key(self):
        """change SHA1 key"""
        key1 = self.factory.create_random_key(self)
        key2 = self.factory.create_random_key(self)
        while key2.conf_key_id == key1.conf_key_id:
            key2 = self.factory.create_random_key(self)
        key1.add_vpp_config()
        key2.add_vpp_config()
        session = VppBFDUDPSession(
            self,
            self.interface0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key1,
        )
        session.add_vpp_config()
        session.activate_auth(key2)

    def test_set_del_udp_echo_source(self):
        """set/del udp echo source"""

        if self.multihop:
            self.skipTest(
                f"Skipping because echo functionality is not supported with multihop"
            )

        self.create_loopback_interfaces(1)
        self.loopback0 = self.lo_interfaces[0]
        self.loopback0.admin_up()
        echo_source = self.vapi.bfd_udp_get_echo_source()
        self.assertFalse(echo_source.is_set)
        self.assertFalse(echo_source.have_usable_ip4)
        self.assertFalse(echo_source.have_usable_ip6)

        self.vapi.bfd_udp_set_echo_source(sw_if_index=self.loopback0.sw_if_index)
        echo_source = self.vapi.bfd_udp_get_echo_source()
        self.assertTrue(echo_source.is_set)
        self.assertEqual(echo_source.sw_if_index, self.loopback0.sw_if_index)
        self.assertFalse(echo_source.have_usable_ip4)
        self.assertFalse(echo_source.have_usable_ip6)

        self.loopback0.config_ip4()
        echo_ip4 = ipaddress.IPv4Address(
            int(ipaddress.IPv4Address(self.loopback0.local_ip4)) ^ 1
        ).packed
        echo_source = self.vapi.bfd_udp_get_echo_source()
        self.assertTrue(echo_source.is_set)
        self.assertEqual(echo_source.sw_if_index, self.loopback0.sw_if_index)
        self.assertTrue(echo_source.have_usable_ip4)
        self.assertEqual(echo_source.ip4_addr.packed, echo_ip4)
        self.assertFalse(echo_source.have_usable_ip6)

        self.loopback0.config_ip6()
        echo_ip6 = ipaddress.IPv6Address(
            int(ipaddress.IPv6Address(self.loopback0.local_ip6)) ^ 1
        ).packed

        echo_source = self.vapi.bfd_udp_get_echo_source()
        self.assertTrue(echo_source.is_set)
        self.assertEqual(echo_source.sw_if_index, self.loopback0.sw_if_index)
        self.assertTrue(echo_source.have_usable_ip4)
        self.assertEqual(echo_source.ip4_addr.packed, echo_ip4)
        self.assertTrue(echo_source.have_usable_ip6)
        self.assertEqual(echo_source.ip6_addr.packed, echo_ip6)

        self.vapi.bfd_udp_del_echo_source()
        echo_source = self.vapi.bfd_udp_get_echo_source()
        self.assertFalse(echo_source.is_set)
        self.assertFalse(echo_source.have_usable_ip4)
        self.assertFalse(echo_source.have_usable_ip6)


class BFDTestSession(object):
    """BFD session as seen from test framework side"""

    def __init__(
        self,
        test,
        interface,
        af,
        detect_mult=3,
        sha1_key=None,
        bfd_key_id=None,
        our_seq_number=None,
        tunnel_header=None,
        phy_interface=None,
        multihop=False,
        local_addr=None,
        peer_addr=None,
    ):
        self.test = test
        self.af = af
        self.sha1_key = sha1_key
        self.bfd_key_id = bfd_key_id
        self.interface = interface
        if phy_interface:
            self.phy_interface = phy_interface
        else:
            self.phy_interface = self.interface
        self.udp_sport = randint(49152, 65535)
        if our_seq_number is None:
            self.our_seq_number = randint(0, 40000000)
        else:
            self.our_seq_number = our_seq_number
        self.vpp_seq_number = None
        self.my_discriminator = 0
        self.desired_min_tx = 300000
        self.required_min_rx = 300000
        self.required_min_echo_rx = None
        self.detect_mult = detect_mult
        self.diag = BFDDiagCode.no_diagnostic
        self.your_discriminator = None
        self.state = BFDState.down
        self.auth_type = BFDAuthType.no_auth
        self.tunnel_header = tunnel_header
        self.tx_packets = 0
        self.rx_packets = 0
        self.tx_packets_echo = 0
        self.rx_packets_echo = 0

        if multihop:
            self.sw_if_index = 0
            BFD.udp_dport = BFD_UDP_MH_PORT
        else:
            BFD.udp_dport = BFD_UDP_SH_PORT
            self.sw_if_index = self.interface.sw_if_index

        if local_addr:
            self.local_addr = local_addr
        else:
            if self.af == AF_INET:
                self.local_addr = self.interface.local_ip4
            else:
                self.local_addr = self.interface.local_ip6

        if peer_addr:
            self.peer_addr = peer_addr
        else:
            if self.af == AF_INET:
                self.peer_addr = self.interface.remote_ip4
            else:
                self.peer_addr = self.interface.remote_ip6

    def inc_seq_num(self):
        """increment sequence number, wrapping if needed"""
        if self.our_seq_number == 0xFFFFFFFF:
            self.our_seq_number = 0
        else:
            self.our_seq_number += 1

    def update(
        self,
        my_discriminator=None,
        your_discriminator=None,
        desired_min_tx=None,
        required_min_rx=None,
        required_min_echo_rx=None,
        detect_mult=None,
        diag=None,
        state=None,
        auth_type=None,
    ):
        """update BFD parameters associated with session"""
        if my_discriminator is not None:
            self.my_discriminator = my_discriminator
        if your_discriminator is not None:
            self.your_discriminator = your_discriminator
        if required_min_rx is not None:
            self.required_min_rx = required_min_rx
        if required_min_echo_rx is not None:
            self.required_min_echo_rx = required_min_echo_rx
        if desired_min_tx is not None:
            self.desired_min_tx = desired_min_tx
        if detect_mult is not None:
            self.detect_mult = detect_mult
        if diag is not None:
            self.diag = diag
        if state is not None:
            self.state = state
        if auth_type is not None:
            self.auth_type = auth_type

    def fill_packet_fields(self, packet):
        """set packet fields with known values in packet"""
        bfd = packet[BFD]
        if self.my_discriminator:
            self.test.logger.debug(
                "BFD: setting packet.my_discriminator=%s", self.my_discriminator
            )
            bfd.my_discriminator = self.my_discriminator
        if self.your_discriminator:
            self.test.logger.debug(
                "BFD: setting packet.your_discriminator=%s", self.your_discriminator
            )
            bfd.your_discriminator = self.your_discriminator
        if self.required_min_rx:
            self.test.logger.debug(
                "BFD: setting packet.required_min_rx_interval=%s", self.required_min_rx
            )
            bfd.required_min_rx_interval = self.required_min_rx
        if self.required_min_echo_rx:
            self.test.logger.debug(
                "BFD: setting packet.required_min_echo_rx=%s", self.required_min_echo_rx
            )
            bfd.required_min_echo_rx_interval = self.required_min_echo_rx
        if self.desired_min_tx:
            self.test.logger.debug(
                "BFD: setting packet.desired_min_tx_interval=%s", self.desired_min_tx
            )
            bfd.desired_min_tx_interval = self.desired_min_tx
        if self.detect_mult:
            self.test.logger.debug(
                "BFD: setting packet.detect_mult=%s", self.detect_mult
            )
            bfd.detect_mult = self.detect_mult
        if self.diag:
            self.test.logger.debug("BFD: setting packet.diag=%s", self.diag)
            bfd.diag = self.diag
        if self.state:
            self.test.logger.debug("BFD: setting packet.state=%s", self.state)
            bfd.state = self.state
        if self.auth_type:
            # this is used by a negative test-case
            self.test.logger.debug("BFD: setting packet.auth_type=%s", self.auth_type)
            bfd.auth_type = self.auth_type

    def create_packet(self):
        """create a BFD packet, reflecting the current state of session"""
        if self.sha1_key:
            bfd = BFD(flags="A")
            bfd.auth_type = self.sha1_key.auth_type
            bfd.auth_len = BFD.sha1_auth_len
            bfd.auth_key_id = self.bfd_key_id
            bfd.auth_seq_num = self.our_seq_number
            bfd.length = BFD.sha1_auth_len + BFD.bfd_pkt_len
        else:
            bfd = BFD()
        packet = Ether(
            src=self.phy_interface.remote_mac, dst=self.phy_interface.local_mac
        )
        if self.tunnel_header:
            packet = packet / self.tunnel_header
        if self.af == AF_INET6:
            packet = (
                packet
                / IPv6(
                    src=self.peer_addr,
                    dst=self.local_addr,
                    hlim=255,
                )
                / UDP(sport=self.udp_sport, dport=BFD.udp_dport)
                / bfd
            )
        else:
            packet = (
                packet
                / IP(src=self.peer_addr, dst=self.local_addr, ttl=255)
                / UDP(sport=self.udp_sport, dport=BFD.udp_dport)
                / bfd
            )
        self.test.logger.debug("BFD: Creating packet")
        self.fill_packet_fields(packet)
        if self.sha1_key:
            hash_material = (
                scapy.compat.raw(packet[BFD])[:32]
                + self.sha1_key.key
                + b"\0" * (20 - len(self.sha1_key.key))
            )
            self.test.logger.debug(
                "BFD: Calculated SHA1 hash: %s"
                % hashlib.sha1(hash_material).hexdigest()
            )
            packet[BFD].auth_key_hash = hashlib.sha1(hash_material).digest()
        return packet

    def send_packet(self, packet=None, interface=None):
        """send packet on interface, creating the packet if needed"""
        if packet is None:
            packet = self.create_packet()
        if interface is None:
            interface = self.phy_interface
        self.test.logger.debug(ppp("Sending packet:", packet))
        interface.add_stream(packet)
        self.tx_packets += 1
        self.test.pg_start()

    def verify_sha1_auth(self, packet):
        """Verify correctness of authentication in BFD layer."""
        bfd = packet[BFD]
        self.test.assert_equal(bfd.auth_len, 28, "Auth section length")
        self.test.assert_equal(bfd.auth_type, self.sha1_key.auth_type, BFDAuthType)
        self.test.assert_equal(bfd.auth_key_id, self.bfd_key_id, "Key ID")
        self.test.assert_equal(bfd.auth_reserved, 0, "Reserved")
        if self.vpp_seq_number is None:
            self.vpp_seq_number = bfd.auth_seq_num
            self.test.logger.debug(
                "Received initial sequence number: %s" % self.vpp_seq_number
            )
        else:
            recvd_seq_num = bfd.auth_seq_num
            self.test.logger.debug(
                "Received followup sequence number: %s" % recvd_seq_num
            )
            if self.vpp_seq_number < 0xFFFFFFFF:
                if self.sha1_key.auth_type == BFDAuthType.meticulous_keyed_sha1:
                    self.test.assert_equal(
                        recvd_seq_num, self.vpp_seq_number + 1, "BFD sequence number"
                    )
                else:
                    self.test.assert_in_range(
                        recvd_seq_num,
                        self.vpp_seq_number,
                        self.vpp_seq_number + 1,
                        "BFD sequence number",
                    )
            else:
                if self.sha1_key.auth_type == BFDAuthType.meticulous_keyed_sha1:
                    self.test.assert_equal(recvd_seq_num, 0, "BFD sequence number")
                else:
                    self.test.assertIn(
                        recvd_seq_num,
                        (self.vpp_seq_number, 0),
                        "BFD sequence number not one of "
                        "(%s, 0)" % self.vpp_seq_number,
                    )
            self.vpp_seq_number = recvd_seq_num
        # last 20 bytes represent the hash - so replace them with the key,
        # pad the result with zeros and hash the result
        hash_material = (
            bfd.original[:-20]
            + self.sha1_key.key
            + b"\0" * (20 - len(self.sha1_key.key))
        )
        expected_hash = hashlib.sha1(hash_material).hexdigest()
        self.test.assert_equal(
            binascii.hexlify(bfd.auth_key_hash), expected_hash.encode(), "Auth key hash"
        )

    def verify_bfd(self, packet):
        """Verify correctness of BFD layer."""
        bfd = packet[BFD]
        self.test.assert_equal(bfd.version, 1, "BFD version")
        self.test.assert_equal(
            bfd.your_discriminator, self.my_discriminator, "BFD - your discriminator"
        )
        if self.sha1_key:
            self.verify_sha1_auth(packet)


def bfd_session_up(test):
    """Bring BFD session up"""
    test.logger.info("BFD: Waiting for slow hello")
    p = wait_for_bfd_packet(test, 2, is_tunnel=test.vpp_session.is_tunnel)
    old_offset = None
    if hasattr(test, "vpp_clock_offset"):
        old_offset = test.vpp_clock_offset
    test.vpp_clock_offset = time.time() - float(p.time)
    test.logger.debug("BFD: Calculated vpp clock offset: %s", test.vpp_clock_offset)
    if old_offset:
        test.assertAlmostEqual(
            old_offset,
            test.vpp_clock_offset,
            delta=0.5,
            msg="vpp clock offset not stable (new: %s, old: %s)"
            % (test.vpp_clock_offset, old_offset),
        )
    test.logger.info("BFD: Sending Init")
    test.test_session.update(
        my_discriminator=randint(0, 40000000),
        your_discriminator=p[BFD].my_discriminator,
        state=BFDState.init,
    )
    if (
        test.test_session.sha1_key
        and test.test_session.sha1_key.auth_type == BFDAuthType.meticulous_keyed_sha1
    ):
        test.test_session.inc_seq_num()
    test.test_session.send_packet()
    test.logger.info("BFD: Waiting for event")
    e = test.vapi.wait_for_event(1, "bfd_udp_session_event")
    verify_event(test, e, expected_state=BFDState.up)
    test.logger.info("BFD: Session is Up")
    test.test_session.update(state=BFDState.up)
    if (
        test.test_session.sha1_key
        and test.test_session.sha1_key.auth_type == BFDAuthType.meticulous_keyed_sha1
    ):
        test.test_session.inc_seq_num()
    test.test_session.send_packet()
    test.assert_equal(test.vpp_session.state, BFDState.up, BFDState)


def bfd_session_down(test):
    """Bring BFD session down"""
    test.assert_equal(test.vpp_session.state, BFDState.up, BFDState)
    test.test_session.update(state=BFDState.down)
    if (
        test.test_session.sha1_key
        and test.test_session.sha1_key.auth_type == BFDAuthType.meticulous_keyed_sha1
    ):
        test.test_session.inc_seq_num()
    test.test_session.send_packet()
    test.logger.info("BFD: Waiting for event")
    e = test.vapi.wait_for_event(1, "bfd_udp_session_event")
    verify_event(test, e, expected_state=BFDState.down)
    test.logger.info("BFD: Session is Down")
    test.assert_equal(test.vpp_session.state, BFDState.down, BFDState)


def verify_bfd_session_config(test, session, state=None):
    dump = session.get_bfd_udp_session_dump_entry()
    test.assertIsNotNone(dump)
    # since dump is not none, we have verified that sw_if_index and addresses
    # are valid (in get_bfd_udp_session_dump_entry)
    if state:
        test.assert_equal(dump.state, state, "session state")
    test.assert_equal(
        dump.required_min_rx, session.required_min_rx, "required min rx interval"
    )
    test.assert_equal(
        dump.desired_min_tx, session.desired_min_tx, "desired min tx interval"
    )
    test.assert_equal(dump.detect_mult, session.detect_mult, "detect multiplier")
    if session.sha1_key is None:
        test.assert_equal(dump.is_authenticated, 0, "is_authenticated flag")
    else:
        test.assert_equal(dump.is_authenticated, 1, "is_authenticated flag")
        test.assert_equal(dump.bfd_key_id, session.bfd_key_id, "bfd key id")
        test.assert_equal(
            dump.conf_key_id, session.sha1_key.conf_key_id, "config key id"
        )


def verify_ip(test, packet):
    """Verify correctness of IP layer."""
    if test.vpp_session.af == AF_INET6:
        ip = packet[IPv6]
        local_ip = test.vpp_session.local_addr
        remote_ip = test.vpp_session.peer_addr
        test.assert_equal(ip.hlim, 255, "IPv6 hop limit")
    else:
        ip = packet[IP]
        local_ip = test.vpp_session.local_addr
        remote_ip = test.vpp_session.peer_addr
        test.assert_equal(ip.ttl, 255, "IPv4 TTL")
    test.assert_equal(ip.src, local_ip, "IP source address")
    test.assert_equal(ip.dst, remote_ip, "IP destination address")


def verify_udp(test, packet):
    """Verify correctness of UDP layer."""
    udp = packet[UDP]
    test.assert_equal(udp.dport, BFD.udp_dport, "UDP destination port")
    test.assert_in_range(
        udp.sport, BFD.udp_sport_min, BFD.udp_sport_max, "UDP source port"
    )


def verify_event(test, event, expected_state):
    """Verify correctness of event values."""
    e = event
    test.logger.debug("BFD: Event: %s" % reprlib.repr(e))
    test.assert_equal(
        e.sw_if_index, test.vpp_session._sw_if_index, "BFD interface index"
    )

    test.assert_equal(
        str(e.local_addr), test.vpp_session.local_addr, "Local IPv6 address"
    )
    test.assert_equal(str(e.peer_addr), test.vpp_session.peer_addr, "Peer IPv6 address")
    test.assert_equal(e.state, expected_state, BFDState)


def wait_for_bfd_packet(test, timeout=1, pcap_time_min=None, is_tunnel=False):
    """wait for BFD packet and verify its correctness

    :param timeout: how long to wait
    :param pcap_time_min: ignore packets with pcap timestamp lower than this

    :returns: tuple (packet, time spent waiting for packet)
    """
    test.logger.info("BFD: Waiting for BFD packet")
    deadline = time.time() + timeout
    counter = 0
    while True:
        counter += 1
        # sanity check
        test.assert_in_range(counter, 0, 100, "number of packets ignored")
        time_left = deadline - time.time()
        if time_left < 0:
            raise CaptureTimeoutError("Packet did not arrive within timeout")
        p = test.pg0.wait_for_packet(timeout=time_left)
        test.test_session.rx_packets += 1
        test.logger.debug(ppp("BFD: Got packet:", p))
        if pcap_time_min is not None and p.time < pcap_time_min:
            test.logger.debug(
                ppp(
                    "BFD: ignoring packet (pcap time %s < "
                    "pcap time min %s):" % (p.time, pcap_time_min),
                    p,
                )
            )
        else:
            break
    test.logger.debug(test.vapi.ppcli("show trace"))
    if is_tunnel:
        # strip an IP layer and move to the next
        p = p[IP].payload

    bfd = p[BFD]
    if bfd is None:
        raise Exception(ppp("Unexpected or invalid BFD packet:", p))
    if bfd.payload:
        raise Exception(ppp("Unexpected payload in BFD packet:", bfd))
    verify_ip(test, p)
    verify_udp(test, p)
    test.test_session.verify_bfd(p)
    return p


BFDStats = namedtuple("BFDStats", "rx rx_echo tx tx_echo")


def bfd_grab_stats_snapshot(test, bs_idx=0, thread_index=None):
    s = test.statistics
    ti = thread_index
    if ti is None:
        rx = s["/bfd/rx-session-counters"][:, bs_idx].sum_packets()
        rx_echo = s["/bfd/rx-session-echo-counters"][:, bs_idx].sum_packets()
        tx = s["/bfd/tx-session-counters"][:, bs_idx].sum_packets()
        tx_echo = s["/bfd/tx-session-echo-counters"][:, bs_idx].sum_packets()
    else:
        rx = s["/bfd/rx-session-counters"][ti, bs_idx].sum_packets()
        rx_echo = s["/bfd/rx-session-echo-counters"][ti, bs_idx].sum_packets()
        tx = s["/bfd/tx-session-counters"][ti, bs_idx].sum_packets()
        tx_echo = s["/bfd/tx-session-echo-counters"][ti, bs_idx].sum_packets()
    return BFDStats(rx, rx_echo, tx, tx_echo)


def bfd_stats_diff(stats_before, stats_after):
    rx = stats_after.rx - stats_before.rx
    rx_echo = stats_after.rx_echo - stats_before.rx_echo
    tx = stats_after.tx - stats_before.tx
    tx_echo = stats_after.tx_echo - stats_before.tx_echo
    return BFDStats(rx, rx_echo, tx, tx_echo)


@parameterized_class(
    [
        {"multihop": False},
        {"multihop": True},
    ]
)
@tag_run_solo
@tag_fixme_debian11
class BFD4TestCase(VppTestCase):
    pg0 = None
    interface0 = None
    vpp_clock_offset = None
    vpp_session = None
    test_session = None
    src_ip_net = ""
    dst_ip_net = ""

    @classmethod
    def setUpClass(cls):
        super(BFD4TestCase, cls).setUpClass()
        hoptype_str = "MultiHop" if cls.multihop else "SingleHop"
        cls.__doc__ = (
            f"""""Bidirectional Forwarding Detection (BFD) - IPv4, {hoptype_str}"""
        )
        cls.vapi.cli("set log class bfd level debug")
        try:
            cls.create_pg_interfaces([0])
            cls.create_loopback_interfaces(1)
            cls.loopback0 = cls.lo_interfaces[0]
            cls.loopback0.config_ip4()
            cls.loopback0.admin_up()
            cls.pg0.config_ip4()
            cls.pg0.configure_ipv4_neighbors()
            cls.pg0.admin_up()
            cls.pg0.resolve_arp()

        except Exception:
            super(BFD4TestCase, cls).tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        super(BFD4TestCase, cls).tearDownClass()

    def setUp(self):
        super(BFD4TestCase, self).setUp()
        self.factory = AuthKeyFactory()
        self.vapi.want_bfd_events()
        self.pg0.enable_capture()
        set_ipv4_pfx_route_info(self, self.pg0, BFD_IPV4_REMOTE_ADDR, True)

        try:
            if self.multihop:
                self.interface0 = None
                self.bfd_udp4_sessions = self.statistics[BFD_STATS_V4_MH_PATH]
                self.bfd_udp6_sessions = self.statistics[BFD_STATS_V6_MH_PATH]
            else:
                self.interface0 = self.pg0
                self.bfd_udp4_sessions = self.statistics[BFD_STATS_V4_SH_PATH]
                self.bfd_udp6_sessions = self.statistics[BFD_STATS_V6_SH_PATH]

            self.vapi.cli("trace add bfd-process 500")
            self.vpp_session = VppBFDUDPSession(
                self,
                self.interface0,
                self.dst_ip_net,
                local_addr=self.src_ip_net,
                multihop=self.multihop,
            )
            self.vpp_session.add_vpp_config()
            self.vpp_session.admin_up()
            self.test_session = BFDTestSession(
                self,
                self.pg0,
                AF_INET,
                local_addr=self.src_ip_net,
                peer_addr=self.dst_ip_net,
                multihop=self.multihop,
            )
        except BaseException:
            self.vapi.want_bfd_events(enable_disable=0)
            raise

    def tearDown(self):
        if not self.vpp_dead:
            self.vapi.want_bfd_events(enable_disable=0)
        self.vapi.collect_events()  # clear the event queue
        super(BFD4TestCase, self).tearDown()

    def test_session_up(self):
        """bring BFD session up"""
        bfd_session_up(self)
        if self.multihop:
            bfd_udp4_sessions = self.statistics[BFD_STATS_V4_MH_PATH]
            bfd_udp6_sessions = self.statistics[BFD_STATS_V6_MH_PATH]
        else:
            bfd_udp4_sessions = self.statistics[BFD_STATS_V4_SH_PATH]
            bfd_udp6_sessions = self.statistics[BFD_STATS_V6_SH_PATH]

        self.assert_equal(bfd_udp4_sessions - self.bfd_udp4_sessions, 1)
        self.assert_equal(bfd_udp6_sessions, self.bfd_udp6_sessions)

    def test_session_up_by_ip(self):
        """bring BFD session up - first frame looked up by address pair"""
        self.logger.info("BFD: Sending Slow control frame")
        self.test_session.update(my_discriminator=randint(0, 40000000))
        self.test_session.send_packet()
        self.pg0.enable_capture()
        p = self.pg0.wait_for_packet(1)
        self.assert_equal(
            p[BFD].your_discriminator,
            self.test_session.my_discriminator,
            "BFD - your discriminator",
        )
        self.assert_equal(p[BFD].state, BFDState.init, BFDState)
        self.test_session.update(
            your_discriminator=p[BFD].my_discriminator, state=BFDState.up
        )
        self.logger.info("BFD: Waiting for event")
        e = self.vapi.wait_for_event(1, "bfd_udp_session_event")
        verify_event(self, e, expected_state=BFDState.init)
        self.logger.info("BFD: Sending Up")
        self.test_session.send_packet()
        self.logger.info("BFD: Waiting for event")
        e = self.vapi.wait_for_event(1, "bfd_udp_session_event")
        verify_event(self, e, expected_state=BFDState.up)
        self.logger.info("BFD: Session is Up")
        self.test_session.update(state=BFDState.up)
        self.test_session.send_packet()
        self.assert_equal(self.vpp_session.state, BFDState.up, BFDState)

    def test_session_down(self):
        """bring BFD session down"""
        bfd_session_up(self)
        bfd_session_down(self)

    def test_hold_up(self):
        """hold BFD session up"""
        bfd_session_up(self)
        for dummy in range(self.test_session.detect_mult * 2):
            wait_for_bfd_packet(self)
            self.test_session.send_packet()
        self.assert_equal(len(self.vapi.collect_events()), 0, "number of bfd events")

    def test_slow_timer(self):
        """verify slow periodic control frames while session down"""
        packet_count = 3
        self.logger.info("BFD: Waiting for %d BFD packets", packet_count)
        prev_packet = wait_for_bfd_packet(self, 2)
        for dummy in range(packet_count):
            next_packet = wait_for_bfd_packet(self, 2)
            time_diff = next_packet.time - prev_packet.time
            # spec says the range should be <0.75, 1>, allow extra 0.05 margin
            # to work around timing issues
            self.assert_in_range(time_diff, 0.70, 1.05, "time between slow packets")
            prev_packet = next_packet

    def test_zero_remote_min_rx(self):
        """no packets when zero remote required min rx interval"""
        bfd_session_up(self)
        self.test_session.update(required_min_rx=0)
        self.test_session.send_packet()
        for dummy in range(self.test_session.detect_mult):
            self.sleep(
                self.vpp_session.required_min_rx / USEC_IN_SEC,
                "sleep before transmitting bfd packet",
            )
            self.test_session.send_packet()
            try:
                p = wait_for_bfd_packet(self, timeout=0)
                self.logger.error(ppp("Received unexpected packet:", p))
            except CaptureTimeoutError:
                pass
        self.assert_equal(len(self.vapi.collect_events()), 0, "number of bfd events")
        self.test_session.update(required_min_rx=300000)
        for dummy in range(3):
            self.test_session.send_packet()
            wait_for_bfd_packet(
                self, timeout=self.test_session.required_min_rx / USEC_IN_SEC
            )
        self.assert_equal(len(self.vapi.collect_events()), 0, "number of bfd events")

    def test_conn_down(self):
        """verify session goes down after inactivity"""
        bfd_session_up(self)
        detection_time = (
            self.test_session.detect_mult
            * self.vpp_session.required_min_rx
            / USEC_IN_SEC
        )
        self.sleep(detection_time, "waiting for BFD session time-out")
        e = self.vapi.wait_for_event(1, "bfd_udp_session_event")
        verify_event(self, e, expected_state=BFDState.down)

    def test_peer_discr_reset_sess_down(self):
        """peer discriminator reset after session goes down"""
        bfd_session_up(self)
        detection_time = (
            self.test_session.detect_mult
            * self.vpp_session.required_min_rx
            / USEC_IN_SEC
        )
        self.sleep(detection_time, "waiting for BFD session time-out")
        self.test_session.my_discriminator = 0
        wait_for_bfd_packet(self, pcap_time_min=time.time() - self.vpp_clock_offset)

    def test_large_required_min_rx(self):
        """large remote required min rx interval"""
        bfd_session_up(self)
        p = wait_for_bfd_packet(self)
        interval = 3000000
        self.test_session.update(required_min_rx=interval)
        self.test_session.send_packet()
        time_mark = time.time()
        count = 0
        # busy wait here, trying to collect a packet or event, vpp is not
        # allowed to send packets and the session will timeout first - so the
        # Up->Down event must arrive before any packets do
        while time.time() < time_mark + interval / USEC_IN_SEC:
            try:
                p = wait_for_bfd_packet(self, timeout=0)
                # if vpp managed to send a packet before we did the session
                # session update, then that's fine, ignore it
                if p.time < time_mark - self.vpp_clock_offset:
                    continue
                self.logger.error(ppp("Received unexpected packet:", p))
                count += 1
            except CaptureTimeoutError:
                pass
            events = self.vapi.collect_events()
            if len(events) > 0:
                verify_event(self, events[0], BFDState.down)
                break
        self.assert_equal(count, 0, "number of packets received")

    def test_immediate_remote_min_rx_reduction(self):
        """immediately honor remote required min rx reduction"""
        self.vpp_session.remove_vpp_config()
        self.vpp_session = VppBFDUDPSession(
            self,
            self.interface0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            desired_min_tx=10000,
        )
        self.pg0.enable_capture()
        self.vpp_session.add_vpp_config()
        self.test_session.update(desired_min_tx=1000000, required_min_rx=1000000)
        bfd_session_up(self)
        reference_packet = wait_for_bfd_packet(self)
        time_mark = time.time()
        interval = 300000
        self.test_session.update(required_min_rx=interval)
        self.test_session.send_packet()
        extra_time = time.time() - time_mark
        p = wait_for_bfd_packet(self)
        # first packet is allowed to be late by time we spent doing the update
        # calculated in extra_time
        self.assert_in_range(
            p.time - reference_packet.time,
            0.95 * 0.75 * interval / USEC_IN_SEC,
            1.05 * interval / USEC_IN_SEC + extra_time,
            "time between BFD packets",
        )
        reference_packet = p
        for dummy in range(3):
            p = wait_for_bfd_packet(self)
            diff = p.time - reference_packet.time
            self.assert_in_range(
                diff,
                0.95 * 0.75 * interval / USEC_IN_SEC,
                1.05 * interval / USEC_IN_SEC,
                "time between BFD packets",
            )
            reference_packet = p

    def test_modify_req_min_rx_double(self):
        """modify session - double required min rx"""
        bfd_session_up(self)
        p = wait_for_bfd_packet(self)
        self.test_session.update(desired_min_tx=10000, required_min_rx=10000)
        self.test_session.send_packet()
        # double required min rx
        self.vpp_session.modify_parameters(
            required_min_rx=2 * self.vpp_session.required_min_rx
        )
        p = wait_for_bfd_packet(self, pcap_time_min=time.time() - self.vpp_clock_offset)
        # poll bit needs to be set
        self.assertIn("P", p.sprintf("%BFD.flags%"), "Poll bit not set in BFD packet")
        # finish poll sequence with final packet
        final = self.test_session.create_packet()
        final[BFD].flags = "F"
        timeout = (
            self.test_session.detect_mult
            * max(self.test_session.desired_min_tx, self.vpp_session.required_min_rx)
            / USEC_IN_SEC
        )
        self.test_session.send_packet(final)
        time_mark = time.time()
        e = self.vapi.wait_for_event(2 * timeout, "bfd_udp_session_event")
        verify_event(self, e, expected_state=BFDState.down)
        time_to_event = time.time() - time_mark
        self.assert_in_range(
            time_to_event, 0.9 * timeout, 1.1 * timeout, "session timeout"
        )

    def test_modify_req_min_rx_halve(self):
        """modify session - halve required min rx"""
        self.vpp_session.modify_parameters(
            required_min_rx=2 * self.vpp_session.required_min_rx
        )
        bfd_session_up(self)
        p = wait_for_bfd_packet(self)
        self.test_session.update(desired_min_tx=10000, required_min_rx=10000)
        self.test_session.send_packet()
        p = wait_for_bfd_packet(self, pcap_time_min=time.time() - self.vpp_clock_offset)
        # halve required min rx
        old_required_min_rx = self.vpp_session.required_min_rx
        self.vpp_session.modify_parameters(
            required_min_rx=self.vpp_session.required_min_rx // 2
        )
        # now we wait 0.8*3*old-req-min-rx and the session should still be up
        self.sleep(
            0.8 * self.vpp_session.detect_mult * old_required_min_rx / USEC_IN_SEC,
            "wait before finishing poll sequence",
        )
        self.assert_equal(len(self.vapi.collect_events()), 0, "number of bfd events")
        p = wait_for_bfd_packet(self)
        # poll bit needs to be set
        self.assertIn("P", p.sprintf("%BFD.flags%"), "Poll bit not set in BFD packet")
        # finish poll sequence with final packet
        final = self.test_session.create_packet()
        final[BFD].flags = "F"
        self.test_session.send_packet(final)
        # now the session should time out under new conditions
        detection_time = (
            self.test_session.detect_mult
            * self.vpp_session.required_min_rx
            / USEC_IN_SEC
        )
        before = time.time()
        e = self.vapi.wait_for_event(2 * detection_time, "bfd_udp_session_event")
        after = time.time()
        self.assert_in_range(
            after - before,
            0.9 * detection_time,
            1.1 * detection_time,
            "time before bfd session goes down",
        )
        verify_event(self, e, expected_state=BFDState.down)

    def test_modify_detect_mult(self):
        """modify detect multiplier"""
        bfd_session_up(self)
        p = wait_for_bfd_packet(self)
        self.vpp_session.modify_parameters(detect_mult=1)
        p = wait_for_bfd_packet(self, pcap_time_min=time.time() - self.vpp_clock_offset)
        self.assert_equal(
            self.vpp_session.detect_mult, p[BFD].detect_mult, "detect mult"
        )
        # poll bit must not be set
        self.assertNotIn(
            "P", p.sprintf("%BFD.flags%"), "Poll bit not set in BFD packet"
        )
        self.vpp_session.modify_parameters(detect_mult=10)
        p = wait_for_bfd_packet(self, pcap_time_min=time.time() - self.vpp_clock_offset)
        self.assert_equal(
            self.vpp_session.detect_mult, p[BFD].detect_mult, "detect mult"
        )
        # poll bit must not be set
        self.assertNotIn(
            "P", p.sprintf("%BFD.flags%"), "Poll bit not set in BFD packet"
        )

    def test_queued_poll(self):
        """test poll sequence queueing"""
        bfd_session_up(self)
        p = wait_for_bfd_packet(self)
        self.vpp_session.modify_parameters(
            required_min_rx=2 * self.vpp_session.required_min_rx
        )
        p = wait_for_bfd_packet(self)
        poll_sequence_start = time.time()
        poll_sequence_length_min = 0.5
        send_final_after = time.time() + poll_sequence_length_min
        # poll bit needs to be set
        self.assertIn("P", p.sprintf("%BFD.flags%"), "Poll bit not set in BFD packet")
        self.assert_equal(
            p[BFD].required_min_rx_interval,
            self.vpp_session.required_min_rx,
            "BFD required min rx interval",
        )
        self.vpp_session.modify_parameters(
            required_min_rx=2 * self.vpp_session.required_min_rx
        )
        # 2nd poll sequence should be queued now
        # don't send the reply back yet, wait for some time to emulate
        # longer round-trip time
        packet_count = 0
        while time.time() < send_final_after:
            self.test_session.send_packet()
            p = wait_for_bfd_packet(self)
            self.assert_equal(
                len(self.vapi.collect_events()), 0, "number of bfd events"
            )
            self.assert_equal(
                p[BFD].required_min_rx_interval,
                self.vpp_session.required_min_rx,
                "BFD required min rx interval",
            )
            packet_count += 1
            # poll bit must be set
            self.assertIn(
                "P", p.sprintf("%BFD.flags%"), "Poll bit not set in BFD packet"
            )
        final = self.test_session.create_packet()
        final[BFD].flags = "F"
        self.test_session.send_packet(final)
        # finish 1st with final
        poll_sequence_length = time.time() - poll_sequence_start
        # vpp must wait for some time before starting new poll sequence
        poll_no_2_started = False
        for dummy in range(2 * packet_count):
            p = wait_for_bfd_packet(self)
            self.assert_equal(
                len(self.vapi.collect_events()), 0, "number of bfd events"
            )
            if "P" in p.sprintf("%BFD.flags%"):
                poll_no_2_started = True
                if time.time() < poll_sequence_start + poll_sequence_length:
                    raise Exception("VPP started 2nd poll sequence too soon")
                final = self.test_session.create_packet()
                final[BFD].flags = "F"
                self.test_session.send_packet(final)
                break
            else:
                self.test_session.send_packet()
        self.assertTrue(poll_no_2_started, "2nd poll sequence not performed")
        # finish 2nd with final
        final = self.test_session.create_packet()
        final[BFD].flags = "F"
        self.test_session.send_packet(final)
        p = wait_for_bfd_packet(self)
        # poll bit must not be set
        self.assertNotIn("P", p.sprintf("%BFD.flags%"), "Poll bit set in BFD packet")

    # returning inconsistent results requiring retries in per-patch tests
    @unittest.skipUnless(config.extended, "part of extended tests")
    def test_poll_response(self):
        """test correct response to control frame with poll bit set"""
        bfd_session_up(self)
        poll = self.test_session.create_packet()
        poll[BFD].flags = "P"
        self.test_session.send_packet(poll)
        final = wait_for_bfd_packet(
            self, pcap_time_min=time.time() - self.vpp_clock_offset
        )
        self.assertIn("F", final.sprintf("%BFD.flags%"))

    def test_no_periodic_if_remote_demand(self):
        """no periodic frames outside poll sequence if remote demand set"""
        bfd_session_up(self)
        demand = self.test_session.create_packet()
        demand[BFD].flags = "D"
        self.test_session.send_packet(demand)
        transmit_time = (
            0.9
            * max(self.vpp_session.required_min_rx, self.test_session.desired_min_tx)
            / USEC_IN_SEC
        )
        count = 0
        for dummy in range(self.test_session.detect_mult * 2):
            self.sleep(transmit_time)
            self.test_session.send_packet(demand)
            try:
                p = wait_for_bfd_packet(self, timeout=0)
                self.logger.error(ppp("Received unexpected packet:", p))
                count += 1
            except CaptureTimeoutError:
                pass
        events = self.vapi.collect_events()
        for e in events:
            self.logger.error("Received unexpected event: %s", e)
        self.assert_equal(count, 0, "number of packets received")
        self.assert_equal(len(events), 0, "number of events received")

    def test_echo_looped_back(self):
        """echo packets looped back"""

        if self.multihop:
            self.skipTest(
                f"Skipping because echo functionality is not supported with multihop"
            )

        bfd_session_up(self)
        stats_before = bfd_grab_stats_snapshot(self)
        self.pg0.enable_capture()
        echo_packet_count = 10
        # random source port low enough to increment a few times..
        udp_sport_tx = randint(1, 50000)
        udp_sport_rx = udp_sport_tx
        echo_packet = (
            Ether(src=self.pg0.remote_mac, dst=self.pg0.local_mac)
            / IP(src=self.pg0.remote_ip4, dst=self.pg0.remote_ip4)
            / UDP(dport=BFD.udp_dport_echo)
            / Raw("this should be looped back")
        )
        for dummy in range(echo_packet_count):
            self.sleep(0.01, "delay between echo packets")
            echo_packet[UDP].sport = udp_sport_tx
            udp_sport_tx += 1
            self.logger.debug(ppp("Sending packet:", echo_packet))
            self.pg0.add_stream(echo_packet)
            self.pg_start()
            self.logger.debug(self.vapi.ppcli("show trace"))
        counter = 0
        bfd_control_packets_rx = 0
        while counter < echo_packet_count:
            p = self.pg0.wait_for_packet(1)
            self.logger.debug(ppp("Got packet:", p))
            ether = p[Ether]
            self.assert_equal(self.pg0.remote_mac, ether.dst, "Destination MAC")
            self.assert_equal(self.pg0.local_mac, ether.src, "Source MAC")
            ip = p[IP]
            self.assert_equal(self.pg0.remote_ip4, ip.dst, "Destination IP")
            udp = p[UDP]
            if udp.dport == BFD.udp_dport:
                bfd_control_packets_rx += 1
                continue
            self.assert_equal(self.pg0.remote_ip4, ip.src, "Source IP")
            self.assert_equal(udp.dport, BFD.udp_dport_echo, "UDP destination port")
            self.assert_equal(udp.sport, udp_sport_rx, "UDP source port")
            udp_sport_rx += 1
            # need to compare the hex payload here, otherwise BFD_vpp_echo
            # gets in way
            self.assertEqual(
                scapy.compat.raw(p[UDP].payload),
                scapy.compat.raw(echo_packet[UDP].payload),
                "Received packet is not the echo packet sent",
            )
            counter += 1
        self.assert_equal(
            udp_sport_tx,
            udp_sport_rx,
            "UDP source port (== ECHO packet identifier for test purposes)",
        )
        stats_after = bfd_grab_stats_snapshot(self)
        diff = bfd_stats_diff(stats_before, stats_after)
        self.assertEqual(0, diff.rx, "RX counter bumped but no BFD packets sent")
        self.assertEqual(
            0, diff.rx_echo, "RX echo counter bumped but no BFD session exists"
        )
        self.assertEqual(
            0, diff.tx_echo, "TX echo counter bumped but no BFD session exists"
        )

    def test_echo(self):
        """echo function"""

        if self.multihop:
            self.skipTest(
                f"Skipping because echo functionality is not supported with multihop"
            )

        stats_before = bfd_grab_stats_snapshot(self)
        bfd_session_up(self)
        self.test_session.update(required_min_echo_rx=150000)
        self.test_session.send_packet()
        detection_time = (
            self.test_session.detect_mult
            * self.vpp_session.required_min_rx
            / USEC_IN_SEC
        )
        # echo shouldn't work without echo source set
        for dummy in range(10):
            sleep = self.vpp_session.required_min_rx / USEC_IN_SEC
            self.sleep(sleep, "delay before sending bfd packet")
            self.test_session.send_packet()
        p = wait_for_bfd_packet(self, pcap_time_min=time.time() - self.vpp_clock_offset)
        self.assert_equal(
            p[BFD].required_min_rx_interval,
            self.vpp_session.required_min_rx,
            "BFD required min rx interval",
        )
        self.test_session.send_packet()
        self.vapi.bfd_udp_set_echo_source(sw_if_index=self.loopback0.sw_if_index)
        echo_seen = False
        # should be turned on - loopback echo packets
        for dummy in range(3):
            loop_until = time.time() + 0.75 * detection_time
            while time.time() < loop_until:
                p = self.pg0.wait_for_packet(1)
                self.logger.debug(ppp("Got packet:", p))
                if p[UDP].dport == BFD.udp_dport_echo:
                    self.assert_equal(p[IP].dst, self.pg0.local_ip4, "BFD ECHO dst IP")
                    self.assertNotEqual(
                        p[IP].src,
                        self.loopback0.local_ip4,
                        "BFD ECHO src IP equal to loopback IP",
                    )
                    self.logger.debug(ppp("Looping back packet:", p))
                    self.assert_equal(
                        p[Ether].dst,
                        self.pg0.remote_mac,
                        "ECHO packet destination MAC address",
                    )
                    p[Ether].dst = self.pg0.local_mac
                    self.pg0.add_stream(p)
                    self.test_session.rx_packets_echo += 1
                    self.test_session.tx_packets_echo += 1
                    self.pg_start()
                    echo_seen = True
                elif p.haslayer(BFD):
                    self.test_session.rx_packets += 1
                    if echo_seen:
                        self.assertGreaterEqual(
                            p[BFD].required_min_rx_interval, 1000000
                        )
                    if "P" in p.sprintf("%BFD.flags%"):
                        final = self.test_session.create_packet()
                        final[BFD].flags = "F"
                        self.test_session.send_packet(final)
                else:
                    raise Exception(ppp("Received unknown packet:", p))

                self.assert_equal(
                    len(self.vapi.collect_events()), 0, "number of bfd events"
                )
            self.test_session.send_packet()
        self.assertTrue(echo_seen, "No echo packets received")

        stats_after = bfd_grab_stats_snapshot(self)
        diff = bfd_stats_diff(stats_before, stats_after)
        # our rx is vpp tx and vice versa, also tolerate one packet off
        self.assert_in_range(
            self.test_session.tx_packets, diff.rx - 1, diff.rx + 1, "RX counter"
        )
        self.assert_in_range(
            self.test_session.rx_packets, diff.tx - 1, diff.tx + 1, "TX counter"
        )
        self.assert_in_range(
            self.test_session.tx_packets_echo,
            diff.rx_echo - 1,
            diff.rx_echo + 1,
            "RX echo counter",
        )
        self.assert_in_range(
            self.test_session.rx_packets_echo,
            diff.tx_echo - 1,
            diff.tx_echo + 1,
            "TX echo counter",
        )

    def test_echo_fail(self):
        """session goes down if echo function fails"""

        if self.multihop:
            self.skipTest(
                f"Skipping because echo functionality is not supported with multihop"
            )

        bfd_session_up(self)
        self.test_session.update(required_min_echo_rx=150000)
        self.test_session.send_packet()
        detection_time = (
            self.test_session.detect_mult
            * self.vpp_session.required_min_rx
            / USEC_IN_SEC
        )
        self.vapi.bfd_udp_set_echo_source(sw_if_index=self.loopback0.sw_if_index)
        # echo function should be used now, but we will drop the echo packets
        verified_diag = False
        for dummy in range(3):
            loop_until = time.time() + 0.75 * detection_time
            while time.time() < loop_until:
                p = self.pg0.wait_for_packet(1)
                self.logger.debug(ppp("Got packet:", p))
                if p[UDP].dport == BFD.udp_dport_echo:
                    # dropped
                    pass
                elif p.haslayer(BFD):
                    if "P" in p.sprintf("%BFD.flags%"):
                        self.assertGreaterEqual(
                            p[BFD].required_min_rx_interval, 1000000
                        )
                        final = self.test_session.create_packet()
                        final[BFD].flags = "F"
                        self.test_session.send_packet(final)
                    if p[BFD].state == BFDState.down:
                        self.assert_equal(
                            p[BFD].diag, BFDDiagCode.echo_function_failed, BFDDiagCode
                        )
                        verified_diag = True
                else:
                    raise Exception(ppp("Received unknown packet:", p))
            self.test_session.send_packet()
        events = self.vapi.collect_events()
        self.assert_equal(len(events), 1, "number of bfd events")
        self.assert_equal(events[0].state, BFDState.down, BFDState)
        self.assertTrue(verified_diag, "Incorrect diagnostics code received")

    def test_echo_stop(self):
        """echo function stops if peer sets required min echo rx zero"""

        if self.multihop:
            self.skipTest(
                f"Skipping because echo functionality is not supported with multihop"
            )

        bfd_session_up(self)
        self.test_session.update(required_min_echo_rx=150000)
        self.test_session.send_packet()
        self.vapi.bfd_udp_set_echo_source(sw_if_index=self.loopback0.sw_if_index)
        # wait for first echo packet
        while True:
            p = self.pg0.wait_for_packet(1)
            self.logger.debug(ppp("Got packet:", p))
            if p[UDP].dport == BFD.udp_dport_echo:
                self.logger.debug(ppp("Looping back packet:", p))
                p[Ether].dst = self.pg0.local_mac
                self.pg0.add_stream(p)
                self.pg_start()
                break
            elif p.haslayer(BFD):
                # ignore BFD
                pass
            else:
                raise Exception(ppp("Received unknown packet:", p))
        self.test_session.update(required_min_echo_rx=0)
        self.test_session.send_packet()
        # echo packets shouldn't arrive anymore
        for dummy in range(5):
            wait_for_bfd_packet(self, pcap_time_min=time.time() - self.vpp_clock_offset)
            self.test_session.send_packet()
            events = self.vapi.collect_events()
            self.assert_equal(len(events), 0, "number of bfd events")

    def test_echo_source_removed(self):
        """echo function stops if echo source is removed"""

        if self.multihop:
            self.skipTest(
                f"Skipping because echo functionality is not supported with multihop"
            )

        bfd_session_up(self)
        self.test_session.update(required_min_echo_rx=150000)
        self.test_session.send_packet()
        self.vapi.bfd_udp_set_echo_source(sw_if_index=self.loopback0.sw_if_index)
        # wait for first echo packet
        while True:
            p = self.pg0.wait_for_packet(1)
            self.logger.debug(ppp("Got packet:", p))
            if p[UDP].dport == BFD.udp_dport_echo:
                self.logger.debug(ppp("Looping back packet:", p))
                p[Ether].dst = self.pg0.local_mac
                self.pg0.add_stream(p)
                self.pg_start()
                break
            elif p.haslayer(BFD):
                # ignore BFD
                pass
            else:
                raise Exception(ppp("Received unknown packet:", p))
        self.vapi.bfd_udp_del_echo_source()
        self.test_session.send_packet()
        # echo packets shouldn't arrive anymore
        for dummy in range(5):
            wait_for_bfd_packet(self, pcap_time_min=time.time() - self.vpp_clock_offset)
            self.test_session.send_packet()
            events = self.vapi.collect_events()
            self.assert_equal(len(events), 0, "number of bfd events")

    def test_stale_echo(self):
        """stale echo packets don't keep a session up"""

        if self.multihop:
            self.skipTest(
                f"Skipping because echo functionality is not supported with multihop"
            )

        bfd_session_up(self)
        self.test_session.update(required_min_echo_rx=150000)
        self.vapi.bfd_udp_set_echo_source(sw_if_index=self.loopback0.sw_if_index)
        self.test_session.send_packet()
        # should be turned on - loopback echo packets
        echo_packet = None
        timeout_at = None
        timeout_ok = False
        for dummy in range(10 * self.vpp_session.detect_mult):
            p = self.pg0.wait_for_packet(1)
            if p[UDP].dport == BFD.udp_dport_echo:
                if echo_packet is None:
                    self.logger.debug(ppp("Got first echo packet:", p))
                    echo_packet = p
                    timeout_at = (
                        time.time()
                        + self.vpp_session.detect_mult
                        * self.test_session.required_min_echo_rx
                        / USEC_IN_SEC
                    )
                else:
                    self.logger.debug(ppp("Got followup echo packet:", p))
                self.logger.debug(ppp("Looping back first echo packet:", p))
                echo_packet[Ether].dst = self.pg0.local_mac
                self.pg0.add_stream(echo_packet)
                self.pg_start()
            elif p.haslayer(BFD):
                self.logger.debug(ppp("Got packet:", p))
                if "P" in p.sprintf("%BFD.flags%"):
                    final = self.test_session.create_packet()
                    final[BFD].flags = "F"
                    self.test_session.send_packet(final)
                if p[BFD].state == BFDState.down:
                    self.assertIsNotNone(
                        timeout_at,
                        "Session went down before first echo packet received",
                    )
                    now = time.time()
                    self.assertGreaterEqual(
                        now,
                        timeout_at,
                        "Session timeout at %s, but is expected at %s"
                        % (now, timeout_at),
                    )
                    self.assert_equal(
                        p[BFD].diag, BFDDiagCode.echo_function_failed, BFDDiagCode
                    )
                    events = self.vapi.collect_events()
                    self.assert_equal(len(events), 1, "number of bfd events")
                    self.assert_equal(events[0].state, BFDState.down, BFDState)
                    timeout_ok = True
                    break
            else:
                raise Exception(ppp("Received unknown packet:", p))
            self.test_session.send_packet()
        self.assertTrue(timeout_ok, "Expected timeout event didn't occur")

    def test_invalid_echo_checksum(self):
        """echo packets with invalid checksum don't keep a session up"""

        if self.multihop:
            self.skipTest(
                f"Skipping because echo functionality is not supported with multihop"
            )

        bfd_session_up(self)
        self.test_session.update(required_min_echo_rx=150000)
        self.vapi.bfd_udp_set_echo_source(sw_if_index=self.loopback0.sw_if_index)
        self.test_session.send_packet()
        # should be turned on - loopback echo packets
        timeout_at = None
        timeout_ok = False
        for dummy in range(10 * self.vpp_session.detect_mult):
            p = self.pg0.wait_for_packet(1)
            if p[UDP].dport == BFD.udp_dport_echo:
                self.logger.debug(ppp("Got echo packet:", p))
                if timeout_at is None:
                    timeout_at = (
                        time.time()
                        + self.vpp_session.detect_mult
                        * self.test_session.required_min_echo_rx
                        / USEC_IN_SEC
                    )
                p[BFD_vpp_echo].checksum = getrandbits(64)
                p[Ether].dst = self.pg0.local_mac
                self.logger.debug(ppp("Looping back modified echo packet:", p))
                self.pg0.add_stream(p)
                self.pg_start()
            elif p.haslayer(BFD):
                self.logger.debug(ppp("Got packet:", p))
                if "P" in p.sprintf("%BFD.flags%"):
                    final = self.test_session.create_packet()
                    final[BFD].flags = "F"
                    self.test_session.send_packet(final)
                if p[BFD].state == BFDState.down:
                    self.assertIsNotNone(
                        timeout_at,
                        "Session went down before first echo packet received",
                    )
                    now = time.time()
                    self.assertGreaterEqual(
                        now,
                        timeout_at,
                        "Session timeout at %s, but is expected at %s"
                        % (now, timeout_at),
                    )
                    self.assert_equal(
                        p[BFD].diag, BFDDiagCode.echo_function_failed, BFDDiagCode
                    )
                    events = self.vapi.collect_events()
                    self.assert_equal(len(events), 1, "number of bfd events")
                    self.assert_equal(events[0].state, BFDState.down, BFDState)
                    timeout_ok = True
                    break
            else:
                raise Exception(ppp("Received unknown packet:", p))
            self.test_session.send_packet()
        self.assertTrue(timeout_ok, "Expected timeout event didn't occur")

    def test_admin_up_down(self):
        """put session admin-up and admin-down"""
        bfd_session_up(self)
        self.vpp_session.admin_down()
        self.pg0.enable_capture()
        e = self.vapi.wait_for_event(1, "bfd_udp_session_event")
        verify_event(self, e, expected_state=BFDState.admin_down)
        for dummy in range(2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.admin_down, BFDState)
        # try to bring session up - shouldn't be possible
        self.test_session.update(state=BFDState.init)
        self.test_session.send_packet()
        for dummy in range(2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.admin_down, BFDState)
        self.vpp_session.admin_up()
        self.test_session.update(state=BFDState.down)
        e = self.vapi.wait_for_event(1, "bfd_udp_session_event")
        verify_event(self, e, expected_state=BFDState.down)
        p = wait_for_bfd_packet(self, pcap_time_min=time.time() - self.vpp_clock_offset)
        self.assert_equal(p[BFD].state, BFDState.down, BFDState)
        self.test_session.send_packet()
        p = wait_for_bfd_packet(self, pcap_time_min=time.time() - self.vpp_clock_offset)
        self.assert_equal(p[BFD].state, BFDState.init, BFDState)
        e = self.vapi.wait_for_event(1, "bfd_udp_session_event")
        verify_event(self, e, expected_state=BFDState.init)
        self.test_session.update(state=BFDState.up)
        self.test_session.send_packet()
        p = wait_for_bfd_packet(self, pcap_time_min=time.time() - self.vpp_clock_offset)
        self.assert_equal(p[BFD].state, BFDState.up, BFDState)
        e = self.vapi.wait_for_event(1, "bfd_udp_session_event")
        verify_event(self, e, expected_state=BFDState.up)

    def test_config_change_remote_demand(self):
        """configuration change while peer in demand mode"""
        bfd_session_up(self)
        demand = self.test_session.create_packet()
        demand[BFD].flags = "D"
        self.test_session.send_packet(demand)
        self.vpp_session.modify_parameters(
            required_min_rx=2 * self.vpp_session.required_min_rx
        )
        p = wait_for_bfd_packet(self, pcap_time_min=time.time() - self.vpp_clock_offset)
        # poll bit must be set
        self.assertIn("P", p.sprintf("%BFD.flags%"), "Poll bit not set")
        # terminate poll sequence
        final = self.test_session.create_packet()
        final[BFD].flags = "D+F"
        self.test_session.send_packet(final)
        # vpp should be quiet now again
        transmit_time = (
            0.9
            * max(self.vpp_session.required_min_rx, self.test_session.desired_min_tx)
            / USEC_IN_SEC
        )
        count = 0
        for dummy in range(self.test_session.detect_mult * 2):
            self.sleep(transmit_time)
            self.test_session.send_packet(demand)
            try:
                p = wait_for_bfd_packet(self, timeout=0)
                self.logger.error(ppp("Received unexpected packet:", p))
                count += 1
            except CaptureTimeoutError:
                pass
        events = self.vapi.collect_events()
        for e in events:
            self.logger.error("Received unexpected event: %s", e)
        self.assert_equal(count, 0, "number of packets received")
        self.assert_equal(len(events), 0, "number of events received")

    def test_intf_deleted(self):
        """interface with bfd session deleted"""
        intf = VppLoInterface(self)
        intf.config_ip4()
        intf.admin_up()
        sw_if_index = intf.sw_if_index

        # Setup routing info for multihop
        set_ipv4_pfx_route_info(self, intf, BFD_IPV4_REMOTE_ADDR2, False)

        vpp_session = VppBFDUDPSession(
            self,
            intf,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
        )

        vpp_session.add_vpp_config()
        vpp_session.admin_up()
        intf.remove_vpp_config()

        # BFD session is removed only for single-hop session
        if self.multihop:
            self.assertTrue(vpp_session.query_vpp_config())
        else:
            e = self.vapi.wait_for_event(1, "bfd_udp_session_event")
            self.assert_equal(e.sw_if_index, sw_if_index, "sw_if_index")
            self.assertFalse(vpp_session.query_vpp_config())


@parameterized_class(
    [
        {"multihop": False},
        {"multihop": True},
    ]
)
@tag_run_solo
@tag_fixme_vpp_workers
class BFD6TestCase(VppTestCase):
    pg0 = None
    interface0 = None
    vpp_clock_offset = None
    vpp_session = None
    test_session = None
    src_ip6_net = ""
    dst_ip6_net = ""

    @classmethod
    def setUpClass(cls):
        super(BFD6TestCase, cls).setUpClass()
        hoptype_str = "MultiHop" if cls.multihop else "SingleHop"
        cls.__doc__ = (
            f"""""Bidirectional Forwarding Detection (BFD) - IPv6, {hoptype_str}"""
        )
        cls.vapi.cli("set log class bfd level debug")
        try:
            cls.create_pg_interfaces([0])
            cls.pg0.config_ip6()
            cls.pg0.configure_ipv6_neighbors()
            cls.pg0.admin_up()
            cls.pg0.resolve_ndp()
            cls.create_loopback_interfaces(1)
            cls.loopback0 = cls.lo_interfaces[0]
            cls.loopback0.config_ip6()
            cls.loopback0.admin_up()

        except Exception:
            super(BFD6TestCase, cls).tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        super(BFD6TestCase, cls).tearDownClass()

    def setUp(self):
        super(BFD6TestCase, self).setUp()
        self.factory = AuthKeyFactory()
        self.vapi.want_bfd_events()
        self.pg0.enable_capture()
        set_ipv6_pfx_route_info(self, self.pg0, BFD_IPV6_REMOTE_ADDR, True)

        try:
            if self.multihop:
                self.interface0 = None
                self.bfd_udp4_sessions = self.statistics[BFD_STATS_V4_MH_PATH]
                self.bfd_udp6_sessions = self.statistics[BFD_STATS_V6_MH_PATH]
            else:
                self.interface0 = self.pg0
                self.bfd_udp4_sessions = self.statistics[BFD_STATS_V4_SH_PATH]
                self.bfd_udp6_sessions = self.statistics[BFD_STATS_V6_SH_PATH]

            self.vpp_session = VppBFDUDPSession(
                self,
                self.interface0,
                self.dst_ip6_net,
                local_addr=self.src_ip6_net,
                af=AF_INET6,
                multihop=self.multihop,
            )
            self.vpp_session.add_vpp_config()
            self.vpp_session.admin_up()
            self.test_session = BFDTestSession(
                self,
                self.pg0,
                AF_INET6,
                local_addr=self.src_ip6_net,
                peer_addr=self.dst_ip6_net,
                multihop=self.multihop,
            )
            self.logger.debug(self.vapi.cli("show adj nbr"))
        except BaseException:
            self.vapi.want_bfd_events(enable_disable=0)
            raise

    def tearDown(self):
        self.vpp_session.remove_vpp_config()
        if not self.vpp_dead:
            self.vapi.want_bfd_events(enable_disable=0)
        self.vapi.collect_events()  # clear the event queue
        super(BFD6TestCase, self).tearDown()

    def test_session_up(self):
        """bring BFD session up"""
        bfd_session_up(self)
        if self.multihop:
            bfd_udp4_sessions = self.statistics[BFD_STATS_V4_MH_PATH]
            bfd_udp6_sessions = self.statistics[BFD_STATS_V6_MH_PATH]
        else:
            bfd_udp4_sessions = self.statistics[BFD_STATS_V4_SH_PATH]
            bfd_udp6_sessions = self.statistics[BFD_STATS_V6_SH_PATH]

        self.assert_equal(bfd_udp4_sessions, self.bfd_udp4_sessions)
        self.assert_equal(bfd_udp6_sessions - self.bfd_udp6_sessions, 1)

    def test_session_up_by_ip(self):
        """bring BFD session up - first frame looked up by address pair"""
        self.logger.info("BFD: Sending Slow control frame")
        self.test_session.update(my_discriminator=randint(0, 40000000))
        self.test_session.send_packet()
        self.pg0.enable_capture()
        p = self.pg0.wait_for_packet(1)
        self.assert_equal(
            p[BFD].your_discriminator,
            self.test_session.my_discriminator,
            "BFD - your discriminator",
        )
        self.assert_equal(p[BFD].state, BFDState.init, BFDState)
        self.test_session.update(
            your_discriminator=p[BFD].my_discriminator, state=BFDState.up
        )
        self.logger.info("BFD: Waiting for event")
        e = self.vapi.wait_for_event(1, "bfd_udp_session_event")
        verify_event(self, e, expected_state=BFDState.init)
        self.logger.info("BFD: Sending Up")
        self.test_session.send_packet()
        self.logger.info("BFD: Waiting for event")
        e = self.vapi.wait_for_event(1, "bfd_udp_session_event")
        verify_event(self, e, expected_state=BFDState.up)
        self.logger.info("BFD: Session is Up")
        self.test_session.update(state=BFDState.up)
        self.test_session.send_packet()
        self.assert_equal(self.vpp_session.state, BFDState.up, BFDState)

    def test_hold_up(self):
        """hold BFD session up"""
        bfd_session_up(self)
        for dummy in range(self.test_session.detect_mult * 2):
            wait_for_bfd_packet(self)
            self.test_session.send_packet()
        self.assert_equal(len(self.vapi.collect_events()), 0, "number of bfd events")
        self.assert_equal(self.vpp_session.state, BFDState.up, BFDState)

    def test_echo_looped_back(self):
        """echo packets looped back"""

        if self.multihop:
            self.skipTest(
                f"Skipping because echo functionality is not supported with multihop"
            )

        bfd_session_up(self)
        stats_before = bfd_grab_stats_snapshot(self)
        self.pg0.enable_capture()
        echo_packet_count = 10
        # random source port low enough to increment a few times..
        udp_sport_tx = randint(1, 50000)
        udp_sport_rx = udp_sport_tx
        echo_packet = (
            Ether(src=self.pg0.remote_mac, dst=self.pg0.local_mac)
            / IPv6(src=self.pg0.remote_ip6, dst=self.pg0.remote_ip6)
            / UDP(dport=BFD.udp_dport_echo)
            / Raw("this should be looped back")
        )
        for dummy in range(echo_packet_count):
            self.sleep(0.01, "delay between echo packets")
            echo_packet[UDP].sport = udp_sport_tx
            udp_sport_tx += 1
            self.logger.debug(ppp("Sending packet:", echo_packet))
            self.pg0.add_stream(echo_packet)
            self.pg_start()
        counter = 0
        bfd_control_packets_rx = 0
        while counter < echo_packet_count:
            p = self.pg0.wait_for_packet(1)
            self.logger.debug(ppp("Got packet:", p))
            ether = p[Ether]
            self.assert_equal(self.pg0.remote_mac, ether.dst, "Destination MAC")
            self.assert_equal(self.pg0.local_mac, ether.src, "Source MAC")
            ip = p[IPv6]
            self.assert_equal(self.pg0.remote_ip6, ip.dst, "Destination IP")
            udp = p[UDP]
            if udp.dport == BFD.udp_dport:
                bfd_control_packets_rx += 1
                continue
            self.assert_equal(self.pg0.remote_ip6, ip.src, "Source IP")
            self.assert_equal(udp.dport, BFD.udp_dport_echo, "UDP destination port")
            self.assert_equal(udp.sport, udp_sport_rx, "UDP source port")
            udp_sport_rx += 1
            # need to compare the hex payload here, otherwise BFD_vpp_echo
            # gets in way
            self.assertEqual(
                scapy.compat.raw(p[UDP].payload),
                scapy.compat.raw(echo_packet[UDP].payload),
                "Received packet is not the echo packet sent",
            )
            counter += 1
        self.assert_equal(
            udp_sport_tx,
            udp_sport_rx,
            "UDP source port (== ECHO packet identifier for test purposes)",
        )
        stats_after = bfd_grab_stats_snapshot(self)
        diff = bfd_stats_diff(stats_before, stats_after)
        self.assertEqual(0, diff.rx, "RX counter bumped but no BFD packets sent")
        self.assertEqual(
            0, diff.rx_echo, "RX echo counter bumped but no BFD session exists"
        )
        self.assertEqual(
            0, diff.tx_echo, "TX echo counter bumped but no BFD session exists"
        )

    def test_echo(self):
        """echo function"""

        if self.multihop:
            self.skipTest(
                f"Skipping because echo functionality is not supported with multihop"
            )

        stats_before = bfd_grab_stats_snapshot(self)
        bfd_session_up(self)
        self.test_session.update(required_min_echo_rx=150000)
        self.test_session.send_packet()
        detection_time = (
            self.test_session.detect_mult
            * self.vpp_session.required_min_rx
            / USEC_IN_SEC
        )
        # echo shouldn't work without echo source set
        for dummy in range(10):
            sleep = self.vpp_session.required_min_rx / USEC_IN_SEC
            self.sleep(sleep, "delay before sending bfd packet")
            self.test_session.send_packet()
        p = wait_for_bfd_packet(self, pcap_time_min=time.time() - self.vpp_clock_offset)
        self.assert_equal(
            p[BFD].required_min_rx_interval,
            self.vpp_session.required_min_rx,
            "BFD required min rx interval",
        )
        self.test_session.send_packet()
        self.vapi.bfd_udp_set_echo_source(sw_if_index=self.loopback0.sw_if_index)
        echo_seen = False
        # should be turned on - loopback echo packets
        for dummy in range(3):
            loop_until = time.time() + 0.75 * detection_time
            while time.time() < loop_until:
                p = self.pg0.wait_for_packet(1)
                self.logger.debug(ppp("Got packet:", p))
                if p[UDP].dport == BFD.udp_dport_echo:
                    self.assert_equal(
                        p[IPv6].dst, self.pg0.local_ip6, "BFD ECHO dst IP"
                    )
                    self.assertNotEqual(
                        p[IPv6].src,
                        self.loopback0.local_ip6,
                        "BFD ECHO src IP equal to loopback IP",
                    )
                    self.logger.debug(ppp("Looping back packet:", p))
                    self.assert_equal(
                        p[Ether].dst,
                        self.pg0.remote_mac,
                        "ECHO packet destination MAC address",
                    )
                    self.test_session.rx_packets_echo += 1
                    self.test_session.tx_packets_echo += 1
                    p[Ether].dst = self.pg0.local_mac
                    self.pg0.add_stream(p)
                    self.pg_start()
                    echo_seen = True
                elif p.haslayer(BFD):
                    self.test_session.rx_packets += 1
                    if echo_seen:
                        self.assertGreaterEqual(
                            p[BFD].required_min_rx_interval, 1000000
                        )
                    if "P" in p.sprintf("%BFD.flags%"):
                        final = self.test_session.create_packet()
                        final[BFD].flags = "F"
                        self.test_session.send_packet(final)
                else:
                    raise Exception(ppp("Received unknown packet:", p))

                self.assert_equal(
                    len(self.vapi.collect_events()), 0, "number of bfd events"
                )
            self.test_session.send_packet()
        self.assertTrue(echo_seen, "No echo packets received")

        stats_after = bfd_grab_stats_snapshot(self)
        diff = bfd_stats_diff(stats_before, stats_after)
        # our rx is vpp tx and vice versa, also tolerate one packet off
        self.assert_in_range(
            self.test_session.tx_packets, diff.rx - 1, diff.rx + 1, "RX counter"
        )
        self.assert_in_range(
            self.test_session.rx_packets, diff.tx - 1, diff.tx + 1, "TX counter"
        )
        self.assert_in_range(
            self.test_session.tx_packets_echo,
            diff.rx_echo - 1,
            diff.rx_echo + 1,
            "RX echo counter",
        )
        self.assert_in_range(
            self.test_session.rx_packets_echo,
            diff.tx_echo - 1,
            diff.tx_echo + 1,
            "TX echo counter",
        )

    def test_intf_deleted(self):
        """interface with bfd session deleted"""
        intf = VppLoInterface(self)
        intf.config_ip6()
        intf.admin_up()
        sw_if_index = intf.sw_if_index

        # Setup routing info for multihop
        set_ipv6_pfx_route_info(self, intf, BFD_IPV6_REMOTE_ADDR2, False)

        vpp_session = VppBFDUDPSession(
            self,
            intf,
            self.dst_ip6_net,
            local_addr=self.src_ip6_net,
            af=AF_INET6,
            multihop=self.multihop,
        )

        vpp_session.add_vpp_config()
        vpp_session.admin_up()
        intf.remove_vpp_config()

        # BFD session is removed only for single-hop session
        if self.multihop:
            self.assertTrue(vpp_session.query_vpp_config())
        else:
            e = self.vapi.wait_for_event(1, "bfd_udp_session_event")
            self.assert_equal(e.sw_if_index, sw_if_index, "sw_if_index")
            self.assertFalse(vpp_session.query_vpp_config())


@parameterized_class(
    [
        {"multihop": False},
        {"multihop": True},
    ]
)
@tag_run_solo
class BFDFIBTestCase(VppTestCase):
    """BFD-FIB interactions (IPv6)"""

    vpp_session = None
    test_session = None
    src_ip6_net = ""
    dst_ip6_net = ""

    @classmethod
    def setUpClass(cls):
        super(BFDFIBTestCase, cls).setUpClass()
        hoptype_str = "MultiHop" if cls.multihop else "SingleHop"
        cls.__doc__ = f"""""BFD-FIB interactions (IPv6) - {hoptype_str}"""
        cls.vapi.cli("set log class bfd level debug")

    @classmethod
    def tearDownClass(cls):
        super(BFDFIBTestCase, cls).tearDownClass()

    def setUp(self):
        super(BFDFIBTestCase, self).setUp()
        self.create_pg_interfaces(range(1))
        self.create_loopback_interfaces(1)
        self.loopback0 = self.lo_interfaces[0]
        self.loopback0.config_ip6()
        self.loopback0.admin_up()

        self.vapi.want_bfd_events()
        self.pg0.enable_capture()

        for i in self.pg_interfaces:
            i.admin_up()
            i.config_ip6()
            i.configure_ipv6_neighbors()

        set_ipv6_pfx_route_info(self, self.pg0, BFD_IPV6_REMOTE_ADDR, True)

    def tearDown(self):
        if not self.vpp_dead:
            self.vapi.want_bfd_events(enable_disable=False)

        super(BFDFIBTestCase, self).tearDown()

    @staticmethod
    def pkt_is_not_data_traffic(p):
        """not data traffic implies BFD or the usual IPv6 ND/RA"""
        if p.haslayer(BFD) or is_ipv6_misc(p):
            return True
        return False

    def test_session_with_fib(self):
        """BFD-FIB interactions"""

        # packets to match against both of the routes
        p = [
            (
                Ether(dst=self.pg0.local_mac, src=self.pg0.remote_mac)
                / IPv6(src="3001::1", dst="2001::1")
                / UDP(sport=1234, dport=1234)
                / Raw(b"\xa5" * 100)
            ),
            (
                Ether(dst=self.pg0.local_mac, src=self.pg0.remote_mac)
                / IPv6(src="3001::1", dst="2002::1")
                / UDP(sport=1234, dport=1234)
                / Raw(b"\xa5" * 100)
            ),
        ]

        # Two different routes via a next-hop that will have a BFD session.
        # One of the routes is non-recursive route only for single-hop.
        if self.multihop:
            intf_index = 0xFFFFFFFF
        else:
            intf_index = self.pg0.sw_if_index

        ip_2001_s_64 = VppIpRoute(
            self,
            "2001::",
            64,
            [VppRoutePath(self.dst_ip6_net, intf_index)],
        )
        ip_2002_s_64 = VppIpRoute(
            self, "2002::", 64, [VppRoutePath(self.dst_ip6_net, 0xFFFFFFFF)]
        )
        ip_2001_s_64.add_vpp_config()
        ip_2002_s_64.add_vpp_config()

        # bring the session up now the routes are present
        self.vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip6_net,
            local_addr=self.src_ip6_net,
            af=AF_INET6,
            multihop=self.multihop,
        )
        self.vpp_session.add_vpp_config()
        self.vpp_session.admin_up()
        self.test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET6,
            local_addr=self.src_ip6_net,
            peer_addr=self.dst_ip6_net,
            multihop=self.multihop,
        )

        # session is up - traffic passes
        bfd_session_up(self)

        self.pg0.add_stream(p)
        self.pg_start()
        for packet in p:
            captured = self.pg0.wait_for_packet(
                1, filter_out_fn=self.pkt_is_not_data_traffic
            )
            self.assertEqual(captured[IPv6].dst, packet[IPv6].dst)

        # session is down - traffic is dropped
        bfd_session_down(self)

        self.pg0.add_stream(p)
        self.pg_start()
        with self.assertRaises(CaptureTimeoutError):
            self.pg0.wait_for_packet(1, self.pkt_is_not_data_traffic)

        # session is up - traffic passes
        bfd_session_up(self)

        self.pg0.add_stream(p)
        self.pg_start()
        for packet in p:
            captured = self.pg0.wait_for_packet(
                1, filter_out_fn=self.pkt_is_not_data_traffic
            )
            self.assertEqual(captured[IPv6].dst, packet[IPv6].dst)


@unittest.skipUnless(config.extended, "part of extended tests")
class BFDTunTestCase(VppTestCase):
    """BFD over GRE tunnel"""

    vpp_session = None
    test_session = None

    @classmethod
    def setUpClass(cls):
        super(BFDTunTestCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(BFDTunTestCase, cls).tearDownClass()

    def setUp(self):
        super(BFDTunTestCase, self).setUp()
        self.create_pg_interfaces(range(1))

        self.vapi.want_bfd_events()
        self.pg0.enable_capture()

        for i in self.pg_interfaces:
            i.admin_up()
            i.config_ip4()
            i.resolve_arp()

    def tearDown(self):
        if not self.vpp_dead:
            self.vapi.want_bfd_events(enable_disable=0)

        super(BFDTunTestCase, self).tearDown()

    @staticmethod
    def pkt_is_not_data_traffic(p):
        """not data traffic implies BFD or the usual IPv6 ND/RA"""
        if p.haslayer(BFD) or is_ipv6_misc(p):
            return True
        return False

    def test_bfd_o_gre(self):
        """BFD-o-GRE"""

        # A GRE interface over which to run a BFD session
        gre_if = VppGreInterface(self, self.pg0.local_ip4, self.pg0.remote_ip4)
        gre_if.add_vpp_config()
        gre_if.admin_up()
        gre_if.config_ip4()

        # bring the session up now the routes are present
        self.vpp_session = VppBFDUDPSession(
            self, gre_if, gre_if.remote_ip4, is_tunnel=True
        )
        self.vpp_session.add_vpp_config()
        self.vpp_session.admin_up()

        self.test_session = BFDTestSession(
            self,
            gre_if,
            AF_INET,
            tunnel_header=(IP(src=self.pg0.remote_ip4, dst=self.pg0.local_ip4) / GRE()),
            phy_interface=self.pg0,
        )

        # packets to match against both of the routes
        p = [
            (
                Ether(dst=self.pg0.local_mac, src=self.pg0.remote_mac)
                / IP(src=self.pg0.remote_ip4, dst=gre_if.remote_ip4)
                / UDP(sport=1234, dport=1234)
                / Raw(b"\xa5" * 100)
            )
        ]

        # session is up - traffic passes
        bfd_session_up(self)

        self.send_and_expect(self.pg0, p, self.pg0)

        # bring session down
        bfd_session_down(self)


@parameterized_class(
    [
        {"multihop": False},
        {"multihop": True},
    ]
)
@tag_run_solo
class BFDSHA1TestCase(VppTestCase):
    pg0 = None
    vpp_clock_offset = None
    vpp_session = None
    test_session = None
    src_ip_net = ""
    dst_ip_net = ""

    @classmethod
    def setUpClass(cls):
        super(BFDSHA1TestCase, cls).setUpClass()
        hoptype_str = "MultiHop" if cls.multihop else "SingleHop"
        cls.__doc__ = f"""""Bidirectional Forwarding Detection (BFD) - SHA1 auth - {hoptype_str}"""
        cls.vapi.cli("set log class bfd level debug")
        try:
            cls.create_pg_interfaces([0])
            cls.create_loopback_interfaces(1)
            cls.loopback0 = cls.lo_interfaces[0]
            cls.loopback0.config_ip4()
            cls.loopback0.admin_up()
            cls.pg0.config_ip4()
            cls.pg0.configure_ipv4_neighbors()
            cls.pg0.admin_up()
            cls.pg0.resolve_arp()

        except Exception:
            super(BFDSHA1TestCase, cls).tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        super(BFDSHA1TestCase, cls).tearDownClass()

    def setUp(self):
        super(BFDSHA1TestCase, self).setUp()
        self.factory = AuthKeyFactory()
        self.vapi.want_bfd_events()
        self.pg0.enable_capture()
        set_ipv4_pfx_route_info(self, self.pg0, BFD_IPV4_REMOTE_ADDR, True)

    def tearDown(self):
        if not self.vpp_dead:
            self.vapi.want_bfd_events(enable_disable=False)
        self.vapi.collect_events()  # clear the event queue
        super(BFDSHA1TestCase, self).tearDown()

    def test_session_up(self):
        """bring BFD session up"""
        key = self.factory.create_random_key(self)
        key.add_vpp_config()
        self.vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
        )
        self.vpp_session.add_vpp_config()
        self.vpp_session.admin_up()
        self.test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
            sha1_key=key,
            bfd_key_id=self.vpp_session.bfd_key_id,
        )
        bfd_session_up(self)

    def test_hold_up(self):
        """hold BFD session up"""
        key = self.factory.create_random_key(self)
        key.add_vpp_config()
        self.vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
        )
        self.vpp_session.add_vpp_config()
        self.vpp_session.admin_up()
        self.test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
            sha1_key=key,
            bfd_key_id=self.vpp_session.bfd_key_id,
        )
        bfd_session_up(self)
        for dummy in range(self.test_session.detect_mult * 2):
            wait_for_bfd_packet(self)
            self.test_session.send_packet()
        self.assert_equal(self.vpp_session.state, BFDState.up, BFDState)

    def test_hold_up_meticulous(self):
        """hold BFD session up - meticulous auth"""
        key = self.factory.create_random_key(self, BFDAuthType.meticulous_keyed_sha1)
        key.add_vpp_config()
        self.vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
        )
        self.vpp_session.add_vpp_config()
        self.vpp_session.admin_up()
        # specify sequence number so that it wraps
        self.test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
            sha1_key=key,
            bfd_key_id=self.vpp_session.bfd_key_id,
            our_seq_number=0xFFFFFFFF - 4,
        )
        bfd_session_up(self)
        for dummy in range(30):
            wait_for_bfd_packet(self)
            self.test_session.inc_seq_num()
            self.test_session.send_packet()
        self.assert_equal(self.vpp_session.state, BFDState.up, BFDState)

    def test_send_bad_seq_number(self):
        """session is not kept alive by msgs with bad sequence numbers"""
        key = self.factory.create_random_key(self, BFDAuthType.meticulous_keyed_sha1)
        key.add_vpp_config()
        self.vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
        )
        self.vpp_session.add_vpp_config()
        self.test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
            sha1_key=key,
            bfd_key_id=self.vpp_session.bfd_key_id,
        )
        bfd_session_up(self)
        detection_time = (
            self.test_session.detect_mult
            * self.vpp_session.required_min_rx
            / USEC_IN_SEC
        )
        send_until = time.time() + 2 * detection_time
        while time.time() < send_until:
            self.test_session.send_packet()
            self.sleep(
                0.7 * self.vpp_session.required_min_rx / USEC_IN_SEC,
                "time between bfd packets",
            )
        e = self.vapi.collect_events()
        # session should be down now, because the sequence numbers weren't
        # updated
        self.assert_equal(len(e), 1, "number of bfd events")
        verify_event(self, e[0], expected_state=BFDState.down)

    def execute_rogue_session_scenario(
        self,
        vpp_bfd_udp_session,
        legitimate_test_session,
        rogue_test_session,
        rogue_bfd_values=None,
    ):
        """execute a rogue session interaction scenario

        1. create vpp session, add config
        2. bring the legitimate session up
        3. copy the bfd values from legitimate session to rogue session
        4. apply rogue_bfd_values to rogue session
        5. set rogue session state to down
        6. send message to take the session down from the rogue session
        7. assert that the legitimate session is unaffected
        """

        self.vpp_session = vpp_bfd_udp_session
        self.vpp_session.add_vpp_config()
        self.test_session = legitimate_test_session
        # bring vpp session up
        bfd_session_up(self)
        # send packet from rogue session
        rogue_test_session.update(
            my_discriminator=self.test_session.my_discriminator,
            your_discriminator=self.test_session.your_discriminator,
            desired_min_tx=self.test_session.desired_min_tx,
            required_min_rx=self.test_session.required_min_rx,
            detect_mult=self.test_session.detect_mult,
            diag=self.test_session.diag,
            state=self.test_session.state,
            auth_type=self.test_session.auth_type,
        )
        if rogue_bfd_values:
            rogue_test_session.update(**rogue_bfd_values)
        rogue_test_session.update(state=BFDState.down)
        rogue_test_session.send_packet()
        wait_for_bfd_packet(self)
        self.assert_equal(self.vpp_session.state, BFDState.up, BFDState)

    def test_mismatch_auth(self):
        """session is not brought down by unauthenticated msg"""
        key = self.factory.create_random_key(self)
        key.add_vpp_config()
        vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
        )
        legitimate_test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
            sha1_key=key,
            bfd_key_id=vpp_session.bfd_key_id,
        )
        rogue_test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
        )
        self.execute_rogue_session_scenario(
            vpp_session, legitimate_test_session, rogue_test_session
        )

    def test_mismatch_bfd_key_id(self):
        """session is not brought down by msg with non-existent key-id"""
        key = self.factory.create_random_key(self)
        key.add_vpp_config()
        vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
        )
        # pick a different random bfd key id
        x = randint(0, 255)
        while x == vpp_session.bfd_key_id:
            x = randint(0, 255)
        legitimate_test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
            sha1_key=key,
            bfd_key_id=vpp_session.bfd_key_id,
        )
        rogue_test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
            sha1_key=key,
            bfd_key_id=x,
        )
        self.execute_rogue_session_scenario(
            vpp_session, legitimate_test_session, rogue_test_session
        )

    def test_mismatched_auth_type(self):
        """session is not brought down by msg with wrong auth type"""
        key = self.factory.create_random_key(self)
        key.add_vpp_config()
        vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
        )
        legitimate_test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
            sha1_key=key,
            bfd_key_id=vpp_session.bfd_key_id,
        )
        rogue_test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
            sha1_key=key,
            bfd_key_id=vpp_session.bfd_key_id,
        )
        self.execute_rogue_session_scenario(
            vpp_session,
            legitimate_test_session,
            rogue_test_session,
            {"auth_type": BFDAuthType.keyed_md5},
        )

    def test_restart(self):
        """simulate remote peer restart and resynchronization"""
        key = self.factory.create_random_key(self, BFDAuthType.meticulous_keyed_sha1)
        key.add_vpp_config()
        self.vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
        )
        self.vpp_session.add_vpp_config()
        self.test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
            sha1_key=key,
            bfd_key_id=self.vpp_session.bfd_key_id,
            our_seq_number=0,
        )
        bfd_session_up(self)
        # don't send any packets for 2*detection_time
        detection_time = (
            self.test_session.detect_mult
            * self.vpp_session.required_min_rx
            / USEC_IN_SEC
        )
        self.sleep(2 * detection_time, "simulating peer restart")
        events = self.vapi.collect_events()
        self.assert_equal(len(events), 1, "number of bfd events")
        verify_event(self, events[0], expected_state=BFDState.down)
        self.test_session.update(state=BFDState.down)
        # reset sequence number
        self.test_session.our_seq_number = 0
        self.test_session.vpp_seq_number = None
        # now throw away any pending packets
        self.pg0.enable_capture()
        self.test_session.my_discriminator = 0
        bfd_session_up(self)


@parameterized_class(
    [
        {"multihop": False},
        {"multihop": True},
    ]
)
@tag_run_solo
class BFDAuthOnOffTestCase(VppTestCase):
    pg0 = None
    vpp_session = None
    test_session = None

    @classmethod
    def setUpClass(cls):
        super(BFDAuthOnOffTestCase, cls).setUpClass()
        hoptype_str = "MultiHop" if cls.multihop else "SingleHop"
        cls.__doc__ = f"""""Bidirectional Forwarding Detection (BFD) - changing auth - {hoptype_str}"""
        cls.vapi.cli("set log class bfd level debug")
        try:
            cls.create_pg_interfaces([0])
            cls.create_loopback_interfaces(1)
            cls.loopback0 = cls.lo_interfaces[0]
            cls.loopback0.config_ip4()
            cls.loopback0.admin_up()
            cls.pg0.config_ip4()
            cls.pg0.configure_ipv4_neighbors()
            cls.pg0.admin_up()
            cls.pg0.resolve_arp()

        except Exception:
            super(BFDAuthOnOffTestCase, cls).tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        super(BFDAuthOnOffTestCase, cls).tearDownClass()

    def setUp(self):
        super(BFDAuthOnOffTestCase, self).setUp()
        self.factory = AuthKeyFactory()
        self.vapi.want_bfd_events()
        self.pg0.enable_capture()
        set_ipv4_pfx_route_info(self, self.pg0, BFD_IPV4_REMOTE_ADDR, True)

    def tearDown(self):
        if not self.vpp_dead:
            self.vapi.want_bfd_events(enable_disable=False)
        self.vapi.collect_events()  # clear the event queue
        super(BFDAuthOnOffTestCase, self).tearDown()

    def test_auth_on_immediate(self):
        """turn auth on without disturbing session state (immediate)"""
        key = self.factory.create_random_key(self)
        key.add_vpp_config()
        self.vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
        )
        self.vpp_session.add_vpp_config()
        self.test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
        )
        bfd_session_up(self)
        for dummy in range(self.test_session.detect_mult * 2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.up, BFDState)
            self.test_session.send_packet()
        self.vpp_session.activate_auth(key)
        self.test_session.bfd_key_id = self.vpp_session.bfd_key_id
        self.test_session.sha1_key = key
        for dummy in range(self.test_session.detect_mult * 2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.up, BFDState)
            self.test_session.send_packet()
        self.assert_equal(self.vpp_session.state, BFDState.up, BFDState)
        self.assert_equal(len(self.vapi.collect_events()), 0, "number of bfd events")

    def test_auth_off_immediate(self):
        """turn auth off without disturbing session state (immediate)"""
        key = self.factory.create_random_key(self)
        key.add_vpp_config()
        self.vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
        )
        self.vpp_session.add_vpp_config()
        self.test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
            sha1_key=key,
            bfd_key_id=self.vpp_session.bfd_key_id,
        )
        bfd_session_up(self)
        # self.vapi.want_bfd_events(enable_disable=0)
        for dummy in range(self.test_session.detect_mult * 2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.up, BFDState)
            self.test_session.inc_seq_num()
            self.test_session.send_packet()
        self.vpp_session.deactivate_auth()
        self.test_session.bfd_key_id = None
        self.test_session.sha1_key = None
        for dummy in range(self.test_session.detect_mult * 2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.up, BFDState)
            self.test_session.inc_seq_num()
            self.test_session.send_packet()
        self.assert_equal(self.vpp_session.state, BFDState.up, BFDState)
        self.assert_equal(len(self.vapi.collect_events()), 0, "number of bfd events")

    def test_auth_change_key_immediate(self):
        """change auth key without disturbing session state (immediate)"""
        key1 = self.factory.create_random_key(self)
        key1.add_vpp_config()
        key2 = self.factory.create_random_key(self)
        key2.add_vpp_config()
        self.vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key1,
        )
        self.vpp_session.add_vpp_config()
        self.test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
            sha1_key=key1,
            bfd_key_id=self.vpp_session.bfd_key_id,
        )
        bfd_session_up(self)
        for dummy in range(self.test_session.detect_mult * 2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.up, BFDState)
            self.test_session.send_packet()
        self.vpp_session.activate_auth(key2)
        self.test_session.bfd_key_id = self.vpp_session.bfd_key_id
        self.test_session.sha1_key = key2
        for dummy in range(self.test_session.detect_mult * 2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.up, BFDState)
            self.test_session.send_packet()
        self.assert_equal(self.vpp_session.state, BFDState.up, BFDState)
        self.assert_equal(len(self.vapi.collect_events()), 0, "number of bfd events")

    def test_auth_on_delayed(self):
        """turn auth on without disturbing session state (delayed)"""
        key = self.factory.create_random_key(self)
        key.add_vpp_config()
        self.vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
        )
        self.vpp_session.add_vpp_config()
        self.test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
        )
        bfd_session_up(self)
        for dummy in range(self.test_session.detect_mult * 2):
            wait_for_bfd_packet(self)
            self.test_session.send_packet()
        self.vpp_session.activate_auth(key, delayed=True)
        for dummy in range(self.test_session.detect_mult * 2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.up, BFDState)
            self.test_session.send_packet()
        self.test_session.bfd_key_id = self.vpp_session.bfd_key_id
        self.test_session.sha1_key = key
        self.test_session.send_packet()
        for dummy in range(self.test_session.detect_mult * 2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.up, BFDState)
            self.test_session.send_packet()
        self.assert_equal(self.vpp_session.state, BFDState.up, BFDState)
        self.assert_equal(len(self.vapi.collect_events()), 0, "number of bfd events")

    def test_auth_off_delayed(self):
        """turn auth off without disturbing session state (delayed)"""
        key = self.factory.create_random_key(self)
        key.add_vpp_config()
        self.vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
        )
        self.vpp_session.add_vpp_config()
        self.test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
            sha1_key=key,
            bfd_key_id=self.vpp_session.bfd_key_id,
        )
        bfd_session_up(self)
        for dummy in range(self.test_session.detect_mult * 2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.up, BFDState)
            self.test_session.send_packet()
        self.vpp_session.deactivate_auth(delayed=True)
        for dummy in range(self.test_session.detect_mult * 2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.up, BFDState)
            self.test_session.send_packet()
        self.test_session.bfd_key_id = None
        self.test_session.sha1_key = None
        self.test_session.send_packet()
        for dummy in range(self.test_session.detect_mult * 2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.up, BFDState)
            self.test_session.send_packet()
        self.assert_equal(self.vpp_session.state, BFDState.up, BFDState)
        self.assert_equal(len(self.vapi.collect_events()), 0, "number of bfd events")

    def test_auth_change_key_delayed(self):
        """change auth key without disturbing session state (delayed)"""
        key1 = self.factory.create_random_key(self)
        key1.add_vpp_config()
        key2 = self.factory.create_random_key(self)
        key2.add_vpp_config()
        self.vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key1,
        )
        self.vpp_session.add_vpp_config()
        self.vpp_session.admin_up()
        self.test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
            sha1_key=key1,
            bfd_key_id=self.vpp_session.bfd_key_id,
        )
        bfd_session_up(self)
        for dummy in range(self.test_session.detect_mult * 2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.up, BFDState)
            self.test_session.send_packet()
        self.vpp_session.activate_auth(key2, delayed=True)
        for dummy in range(self.test_session.detect_mult * 2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.up, BFDState)
            self.test_session.send_packet()
        self.test_session.bfd_key_id = self.vpp_session.bfd_key_id
        self.test_session.sha1_key = key2
        self.test_session.send_packet()
        for dummy in range(self.test_session.detect_mult * 2):
            p = wait_for_bfd_packet(self)
            self.assert_equal(p[BFD].state, BFDState.up, BFDState)
            self.test_session.send_packet()
        self.assert_equal(self.vpp_session.state, BFDState.up, BFDState)
        self.assert_equal(len(self.vapi.collect_events()), 0, "number of bfd events")


@parameterized_class(
    [
        {"multihop": False},
        {"multihop": True},
    ]
)
@tag_run_solo
class BFDCLITestCase(VppTestCase):
    """Bidirectional Forwarding Detection (BFD) (CLI)"""

    pg0 = None

    @classmethod
    def setUpClass(cls):
        super(BFDCLITestCase, cls).setUpClass()
        hoptype_str = "MultiHop" if cls.multihop else "SingleHop"
        cls.__doc__ = (
            f"""""Bidirectional Forwarding Detection (BFD) - CLI - {hoptype_str}"""
        )
        cls.vapi.cli("set log class bfd level debug")
        try:
            cls.create_pg_interfaces([0])
            cls.create_loopback_interfaces(1)
            cls.loopback0 = cls.lo_interfaces[0]
            cls.loopback0.config_ip4()
            cls.loopback0.config_ip6()
            cls.loopback0.admin_up()
            cls.pg0.config_ip4()
            cls.pg0.config_ip6()
            cls.pg0.configure_ipv4_neighbors()
            cls.pg0.admin_up()
            cls.pg0.resolve_arp()
            cls.pg0.resolve_ndp()

        except Exception:
            super(BFDCLITestCase, cls).tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        super(BFDCLITestCase, cls).tearDownClass()

    def setUp(self):
        super(BFDCLITestCase, self).setUp()
        self.factory = AuthKeyFactory()
        self.pg0.enable_capture()
        set_ipv4_pfx_route_info(self, self.pg0, BFD_IPV4_REMOTE_ADDR, True)
        set_ipv6_pfx_route_info(self, self.pg0, BFD_IPV6_REMOTE_ADDR, True)

        if self.multihop:
            self.cli_str = "multihop"
        else:
            self.cli_str = "interface %s" % self.pg0.name

    def tearDown(self):
        try:
            self.vapi.want_bfd_events(enable_disable=False)
        except UnexpectedApiReturnValueError:
            # some tests aren't subscribed, so this is not an issue
            pass
        self.vapi.collect_events()  # clear the event queue
        super(BFDCLITestCase, self).tearDown()

    def cli_verify_no_response(self, cli):
        """execute a CLI, asserting that the response is empty"""
        self.assert_equal(self.vapi.cli(cli), "", "CLI command response")

    def cli_verify_response(self, cli, expected):
        """execute a CLI, asserting that the response matches expectation"""
        try:
            reply = self.vapi.cli(cli)
        except CliFailedCommandError as cli_error:
            reply = str(cli_error)
        self.assert_equal(reply.strip(), expected, "CLI command response")

    def test_show(self):
        """show commands"""
        k1 = self.factory.create_random_key(self)
        k1.add_vpp_config()
        k2 = self.factory.create_random_key(
            self, auth_type=BFDAuthType.meticulous_keyed_sha1
        )
        k2.add_vpp_config()
        s1 = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
        )
        s1.add_vpp_config()
        s2 = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip6_net,
            local_addr=self.src_ip6_net,
            af=AF_INET6,
            multihop=self.multihop,
            sha1_key=k2,
        )
        s2.add_vpp_config()
        self.logger.info(self.vapi.ppcli("show bfd keys"))
        self.logger.info(self.vapi.ppcli("show bfd sessions"))
        self.logger.info(self.vapi.ppcli("show bfd"))

    def test_set_del_sha1_key(self):
        """set/delete SHA1 auth key"""
        k = self.factory.create_random_key(self)
        self.registry.register(k, self.logger)
        self.cli_verify_no_response(
            "bfd key set conf-key-id %s type keyed-sha1 secret %s"
            % (
                k.conf_key_id,
                "".join("{:02x}".format(scapy.compat.orb(c)) for c in k.key),
            )
        )
        self.assertTrue(k.query_vpp_config())
        self.vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=k,
        )
        self.vpp_session.add_vpp_config()
        self.test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET,
            local_addr=self.src_ip_net,
            peer_addr=self.dst_ip_net,
            multihop=self.multihop,
            sha1_key=k,
            bfd_key_id=self.vpp_session.bfd_key_id,
        )
        self.vapi.want_bfd_events()
        bfd_session_up(self)
        bfd_session_down(self)
        # try to replace the secret for the key - should fail because the key
        # is in-use
        k2 = self.factory.create_random_key(self)
        self.cli_verify_response(
            "bfd key set conf-key-id %s type keyed-sha1 secret %s"
            % (
                k.conf_key_id,
                "".join("{:02x}".format(scapy.compat.orb(c)) for c in k2.key),
            ),
            "bfd key set: `bfd_auth_set_key' API call failed, "
            "rv=-103:BFD object in use",
        )
        # manipulating the session using old secret should still work
        bfd_session_up(self)
        bfd_session_down(self)
        self.vpp_session.remove_vpp_config()
        self.cli_verify_no_response("bfd key del conf-key-id %s" % k.conf_key_id)
        self.assertFalse(k.query_vpp_config())

    def test_set_del_meticulous_sha1_key(self):
        """set/delete meticulous SHA1 auth key"""
        k = self.factory.create_random_key(
            self, auth_type=BFDAuthType.meticulous_keyed_sha1
        )
        self.registry.register(k, self.logger)
        self.cli_verify_no_response(
            "bfd key set conf-key-id %s type meticulous-keyed-sha1 secret %s"
            % (
                k.conf_key_id,
                "".join("{:02x}".format(scapy.compat.orb(c)) for c in k.key),
            )
        )
        self.assertTrue(k.query_vpp_config())
        self.vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip6_net,
            local_addr=self.src_ip6_net,
            af=AF_INET6,
            multihop=self.multihop,
            sha1_key=k,
        )
        self.vpp_session.add_vpp_config()
        self.vpp_session.admin_up()
        self.test_session = BFDTestSession(
            self,
            self.pg0,
            AF_INET6,
            local_addr=self.src_ip6_net,
            peer_addr=self.dst_ip6_net,
            multihop=self.multihop,
            sha1_key=k,
            bfd_key_id=self.vpp_session.bfd_key_id,
        )
        self.vapi.want_bfd_events()
        bfd_session_up(self)
        bfd_session_down(self)
        # try to replace the secret for the key - should fail because the key
        # is in-use
        k2 = self.factory.create_random_key(self)
        self.cli_verify_response(
            "bfd key set conf-key-id %s type keyed-sha1 secret %s"
            % (
                k.conf_key_id,
                "".join("{:02x}".format(scapy.compat.orb(c)) for c in k2.key),
            ),
            "bfd key set: `bfd_auth_set_key' API call failed, "
            "rv=-103:BFD object in use",
        )
        # manipulating the session using old secret should still work
        bfd_session_up(self)
        bfd_session_down(self)
        self.vpp_session.remove_vpp_config()
        self.cli_verify_no_response("bfd key del conf-key-id %s" % k.conf_key_id)
        self.assertFalse(k.query_vpp_config())

    def test_add_mod_del_bfd_udp(self):
        """create/modify/delete IPv4 BFD UDP session"""
        vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
        )
        self.registry.register(vpp_session, self.logger)
        cli_add_cmd = (
            "bfd udp session add %s local-addr %s "
            "peer-addr %s desired-min-tx %s required-min-rx %s "
            "detect-mult %s"
            % (
                self.cli_str,
                self.src_ip_net,
                self.dst_ip_net,
                vpp_session.desired_min_tx,
                vpp_session.required_min_rx,
                vpp_session.detect_mult,
            )
        )
        self.cli_verify_no_response(cli_add_cmd)
        # 2nd add should fail
        self.cli_verify_response(
            cli_add_cmd,
            "bfd udp session add: `bfd_add_add_session' API call"
            " failed, rv=-101:Duplicate BFD object",
        )
        verify_bfd_session_config(self, vpp_session)

        mod_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            required_min_rx=2 * vpp_session.required_min_rx,
            desired_min_tx=3 * vpp_session.desired_min_tx,
            detect_mult=4 * vpp_session.detect_mult,
        )
        self.cli_verify_no_response(
            "bfd udp session mod %s local-addr %s peer-addr %s "
            "desired-min-tx %s required-min-rx %s detect-mult %s"
            % (
                self.cli_str,
                self.src_ip_net,
                self.dst_ip_net,
                mod_session.desired_min_tx,
                mod_session.required_min_rx,
                mod_session.detect_mult,
            )
        )
        verify_bfd_session_config(self, mod_session)
        cli_del_cmd = "bfd udp session del %s local-addr %s peer-addr %s" % (
            self.cli_str,
            self.src_ip_net,
            self.dst_ip_net,
        )
        self.cli_verify_no_response(cli_del_cmd)
        # 2nd del is expected to fail
        self.cli_verify_response(
            cli_del_cmd,
            "bfd udp session del: `bfd_udp_del_session' API call"
            " failed, rv=-102:No such BFD object",
        )
        self.assertFalse(vpp_session.query_vpp_config())

        if self.multihop:
            # specifying multihop and interface should fail
            cli_add_cmd = (
                "bfd udp session add multihop interface %s local-addr 1.1.1.1 "
                "peer-addr 2.2.2.2 desired-min-tx 50000 required-min-rx 1000 "
                "detect-mult 3" % (self.pg0.name)
            )
            self.cli_verify_response(
                cli_add_cmd,
                "bfd udp session add: Incompatible parameter combination, "
                "interface cannot be specified when multihop is enabled",
            )
            cli_mod_cmd = (
                "bfd udp session mod multihop interface %s local-addr 1.1.1.1 "
                "peer-addr 2.2.2.2 desired-min-tx 50000 required-min-rx 1000 "
                "detect-mult 3" % (self.pg0.name)
            )
            self.cli_verify_response(
                cli_mod_cmd,
                "bfd udp session mod: Incompatible parameter combination, "
                "interface cannot be specified when multihop is enabled",
            )
            cli_del_cmd = (
                "bfd udp session del multihop interface %s local-addr 1.1.1.1 "
                "peer-addr 2.2.2.2" % (self.pg0.name)
            )
            self.cli_verify_response(
                cli_del_cmd,
                "bfd udp session del: Incompatible parameter combination, "
                "interface cannot be specified when multihop is enabled",
            )

            # Not specifying multihop or interface should fail
            cli_add_cmd = (
                "bfd udp session add local-addr 1.1.1.1 peer-addr 2.2.2.2 "
                "desired-min-tx 50000 required-min-rx 1000 detect-mult 3"
            )
            self.cli_verify_response(
                cli_add_cmd,
                "bfd udp session add: Incompatible parameter combination, "
                "interface must be set if not multihop",
            )
            cli_mod_cmd = (
                "bfd udp session mod local-addr 1.1.1.1 peer-addr 2.2.2.2 "
                "desired-min-tx 50000 required-min-rx 1000 detect-mult 3"
            )
            self.cli_verify_response(
                cli_mod_cmd,
                "bfd udp session mod: Incompatible parameter combination, "
                "interface must be set if not multihop",
            )
            cli_del_cmd = "bfd udp session del local-addr 1.1.1.1 peer-addr 2.2.2.2 "
            self.cli_verify_response(
                cli_del_cmd,
                "bfd udp session del: Incompatible parameter combination, "
                "interface must be set if not multihop",
            )

    def test_add_mod_del_bfd_udp6(self):
        """create/modify/delete IPv6 BFD UDP session"""
        vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip6_net,
            local_addr=self.src_ip6_net,
            af=AF_INET6,
            multihop=self.multihop,
        )
        self.registry.register(vpp_session, self.logger)
        cli_add_cmd = (
            "bfd udp session add %s local-addr %s "
            "peer-addr %s desired-min-tx %s required-min-rx %s "
            "detect-mult %s"
            % (
                self.cli_str,
                self.src_ip6_net,
                self.dst_ip6_net,
                vpp_session.desired_min_tx,
                vpp_session.required_min_rx,
                vpp_session.detect_mult,
            )
        )
        self.cli_verify_no_response(cli_add_cmd)
        # 2nd add should fail
        self.cli_verify_response(
            cli_add_cmd,
            "bfd udp session add: `bfd_add_add_session' API call"
            " failed, rv=-101:Duplicate BFD object",
        )
        verify_bfd_session_config(self, vpp_session)
        mod_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip6_net,
            local_addr=self.src_ip6_net,
            af=AF_INET6,
            multihop=self.multihop,
            required_min_rx=2 * vpp_session.required_min_rx,
            desired_min_tx=3 * vpp_session.desired_min_tx,
            detect_mult=4 * vpp_session.detect_mult,
        )
        self.cli_verify_no_response(
            "bfd udp session mod %s local-addr %s peer-addr %s "
            "desired-min-tx %s required-min-rx %s detect-mult %s"
            % (
                self.cli_str,
                self.src_ip6_net,
                self.dst_ip6_net,
                mod_session.desired_min_tx,
                mod_session.required_min_rx,
                mod_session.detect_mult,
            )
        )
        verify_bfd_session_config(self, mod_session)
        cli_del_cmd = "bfd udp session del %s local-addr %s peer-addr %s" % (
            self.cli_str,
            self.src_ip6_net,
            self.dst_ip6_net,
        )
        self.cli_verify_no_response(cli_del_cmd)
        # 2nd del is expected to fail
        self.cli_verify_response(
            cli_del_cmd,
            "bfd udp session del: `bfd_udp_del_session' API call"
            " failed, rv=-102:No such BFD object",
        )
        self.assertFalse(vpp_session.query_vpp_config())

    def test_add_mod_del_bfd_udp_auth(self):
        """create/modify/delete IPv4 BFD UDP session (authenticated)"""
        key = self.factory.create_random_key(self)
        key.add_vpp_config()
        vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
        )
        self.registry.register(vpp_session, self.logger)
        cli_add_cmd = (
            "bfd udp session add %s local-addr %s "
            "peer-addr %s desired-min-tx %s required-min-rx %s "
            "detect-mult %s conf-key-id %s bfd-key-id %s"
            % (
                self.cli_str,
                self.src_ip_net,
                self.dst_ip_net,
                vpp_session.desired_min_tx,
                vpp_session.required_min_rx,
                vpp_session.detect_mult,
                key.conf_key_id,
                vpp_session.bfd_key_id,
            )
        )
        self.cli_verify_no_response(cli_add_cmd)
        # 2nd add should fail
        self.cli_verify_response(
            cli_add_cmd,
            "bfd udp session add: `bfd_add_add_session' API call"
            " failed, rv=-101:Duplicate BFD object",
        )
        verify_bfd_session_config(self, vpp_session)
        mod_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
            bfd_key_id=vpp_session.bfd_key_id,
            required_min_rx=2 * vpp_session.required_min_rx,
            desired_min_tx=3 * vpp_session.desired_min_tx,
            detect_mult=4 * vpp_session.detect_mult,
        )
        self.cli_verify_no_response(
            "bfd udp session mod %s local-addr %s peer-addr %s "
            "desired-min-tx %s required-min-rx %s detect-mult %s"
            % (
                self.cli_str,
                self.src_ip_net,
                self.dst_ip_net,
                mod_session.desired_min_tx,
                mod_session.required_min_rx,
                mod_session.detect_mult,
            )
        )
        verify_bfd_session_config(self, mod_session)
        cli_del_cmd = "bfd udp session del %s local-addr %s peer-addr %s" % (
            self.cli_str,
            self.src_ip_net,
            self.dst_ip_net,
        )
        self.cli_verify_no_response(cli_del_cmd)
        # 2nd del is expected to fail
        self.cli_verify_response(
            cli_del_cmd,
            "bfd udp session del: `bfd_udp_del_session' API call"
            " failed, rv=-102:No such BFD object",
        )
        self.assertFalse(vpp_session.query_vpp_config())

    def test_add_mod_del_bfd_udp6_auth(self):
        """create/modify/delete IPv6 BFD UDP session (authenticated)"""
        key = self.factory.create_random_key(
            self, auth_type=BFDAuthType.meticulous_keyed_sha1
        )
        key.add_vpp_config()
        vpp_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip6_net,
            local_addr=self.src_ip6_net,
            af=AF_INET6,
            multihop=self.multihop,
            sha1_key=key,
        )
        self.registry.register(vpp_session, self.logger)
        cli_add_cmd = (
            "bfd udp session add %s local-addr %s "
            "peer-addr %s desired-min-tx %s required-min-rx %s "
            "detect-mult %s conf-key-id %s bfd-key-id %s"
            % (
                self.cli_str,
                self.src_ip6_net,
                self.dst_ip6_net,
                vpp_session.desired_min_tx,
                vpp_session.required_min_rx,
                vpp_session.detect_mult,
                key.conf_key_id,
                vpp_session.bfd_key_id,
            )
        )
        self.cli_verify_no_response(cli_add_cmd)
        # 2nd add should fail
        self.cli_verify_response(
            cli_add_cmd,
            "bfd udp session add: `bfd_add_add_session' API call"
            " failed, rv=-101:Duplicate BFD object",
        )
        verify_bfd_session_config(self, vpp_session)
        mod_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip6_net,
            local_addr=self.src_ip6_net,
            af=AF_INET6,
            multihop=self.multihop,
            sha1_key=key,
            bfd_key_id=vpp_session.bfd_key_id,
            required_min_rx=2 * vpp_session.required_min_rx,
            desired_min_tx=3 * vpp_session.desired_min_tx,
            detect_mult=4 * vpp_session.detect_mult,
        )
        self.cli_verify_no_response(
            "bfd udp session mod %s local-addr %s peer-addr %s "
            "desired-min-tx %s required-min-rx %s detect-mult %s"
            % (
                self.cli_str,
                self.src_ip6_net,
                self.dst_ip6_net,
                mod_session.desired_min_tx,
                mod_session.required_min_rx,
                mod_session.detect_mult,
            )
        )
        verify_bfd_session_config(self, mod_session)
        cli_del_cmd = "bfd udp session del %s local-addr %s peer-addr %s" % (
            self.cli_str,
            self.src_ip6_net,
            self.dst_ip6_net,
        )
        self.cli_verify_no_response(cli_del_cmd)
        # 2nd del is expected to fail
        self.cli_verify_response(
            cli_del_cmd,
            "bfd udp session del: `bfd_udp_del_session' API call"
            " failed, rv=-102:No such BFD object",
        )
        self.assertFalse(vpp_session.query_vpp_config())

    def test_auth_on_off(self):
        """turn authentication on and off"""
        key = self.factory.create_random_key(
            self, auth_type=BFDAuthType.meticulous_keyed_sha1
        )
        key.add_vpp_config()
        session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
        )
        auth_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
        )
        session.add_vpp_config()
        cli_activate = (
            "bfd udp session auth activate %s local-addr %s "
            "peer-addr %s conf-key-id %s bfd-key-id %s"
            % (
                self.cli_str,
                self.src_ip_net,
                self.dst_ip_net,
                key.conf_key_id,
                auth_session.bfd_key_id,
            )
        )
        self.cli_verify_no_response(cli_activate)
        verify_bfd_session_config(self, auth_session)
        self.cli_verify_no_response(cli_activate)
        verify_bfd_session_config(self, auth_session)
        cli_deactivate = (
            "bfd udp session auth deactivate %s local-addr %s "
            "peer-addr %s" % (self.cli_str, self.src_ip_net, self.dst_ip_net)
        )
        self.cli_verify_no_response(cli_deactivate)
        verify_bfd_session_config(self, session)
        self.cli_verify_no_response(cli_deactivate)
        verify_bfd_session_config(self, session)

        if self.multihop:
            # specifying multihop and interface should fail
            cli_activate = (
                "bfd udp session auth activate multihop interface %s "
                "local-addr %s peer-addr %s conf-key-id %s bfd-key-id %s"
                % (
                    self.pg0.name,
                    self.src_ip_net,
                    self.dst_ip_net,
                    key.conf_key_id,
                    auth_session.bfd_key_id,
                )
            )
            self.cli_verify_response(
                cli_activate,
                "bfd udp session auth activate: Incompatible parameter "
                "combination, interface cannot be specified when multihop "
                "is enabled",
            )
            cli_deactivate = (
                "bfd udp session auth deactivate multihop interface %s "
                "local-addr %s peer-addr %s"
                % (
                    self.pg0.name,
                    self.src_ip_net,
                    self.dst_ip_net,
                )
            )
            self.cli_verify_response(
                cli_deactivate,
                "bfd udp session auth deactivate: Incompatible parameter "
                "combination, interface cannot be specified when multihop "
                "is enabled",
            )
            # Not specifying multihop or interface should fail
            cli_activate = (
                "bfd udp session auth activate local-addr %s peer-addr %s "
                "conf-key-id %s bfd-key-id %s"
                % (
                    self.src_ip_net,
                    self.dst_ip_net,
                    key.conf_key_id,
                    auth_session.bfd_key_id,
                )
            )
            self.cli_verify_response(
                cli_activate,
                "bfd udp session auth activate: Incompatible parameter "
                "combination, interface must be set if not multihop",
            )
            cli_deactivate = (
                "bfd udp session auth deactivate local-addr %s peer-addr %s "
                % (
                    self.src_ip_net,
                    self.dst_ip_net,
                )
            )
            self.cli_verify_response(
                cli_deactivate,
                "bfd udp session auth deactivate: Incompatible parameter "
                "combination, interface must be set if not multihop",
            )

    def test_auth_on_off_delayed(self):
        """turn authentication on and off (delayed)"""
        key = self.factory.create_random_key(
            self, auth_type=BFDAuthType.meticulous_keyed_sha1
        )
        key.add_vpp_config()
        session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
        )
        auth_session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
            sha1_key=key,
        )
        session.add_vpp_config()
        cli_activate = (
            "bfd udp session auth activate %s local-addr %s "
            "peer-addr %s conf-key-id %s bfd-key-id %s delayed yes"
            % (
                self.cli_str,
                self.src_ip_net,
                self.dst_ip_net,
                key.conf_key_id,
                auth_session.bfd_key_id,
            )
        )
        self.cli_verify_no_response(cli_activate)
        verify_bfd_session_config(self, auth_session)
        self.cli_verify_no_response(cli_activate)
        verify_bfd_session_config(self, auth_session)
        cli_deactivate = (
            "bfd udp session auth deactivate %s local-addr %s "
            "peer-addr %s delayed yes"
            % (self.cli_str, self.src_ip_net, self.dst_ip_net)
        )
        self.cli_verify_no_response(cli_deactivate)
        verify_bfd_session_config(self, session)
        self.cli_verify_no_response(cli_deactivate)
        verify_bfd_session_config(self, session)

    def test_admin_up_down(self):
        """put session admin-up and admin-down"""
        session = VppBFDUDPSession(
            self,
            self.pg0,
            self.dst_ip_net,
            local_addr=self.src_ip_net,
            multihop=self.multihop,
        )
        session.add_vpp_config()
        cli_down = (
            "bfd udp session set-flags admin down %s local-addr %s "
            "peer-addr %s " % (self.cli_str, self.src_ip_net, self.dst_ip_net)
        )
        cli_up = (
            "bfd udp session set-flags admin up %s local-addr %s "
            "peer-addr %s " % (self.cli_str, self.src_ip_net, self.dst_ip_net)
        )
        self.cli_verify_no_response(cli_down)
        verify_bfd_session_config(self, session, state=BFDState.admin_down)
        self.cli_verify_no_response(cli_up)
        verify_bfd_session_config(self, session, state=BFDState.down)

        if self.multihop:
            # specifying multihop and interface should fail
            cli_up = (
                "bfd udp session set-flags admin up multihop interface %s "
                "local-addr %s peer-addr %s"
                % (
                    self.pg0.name,
                    self.src_ip_net,
                    self.dst_ip_net,
                )
            )
            self.cli_verify_response(
                cli_up,
                "bfd udp session set-flags: Incompatible parameter "
                "combination, interface cannot be specified when multihop "
                "is enabled",
            )
            # Not specifying multihop or interface should fail
            cli_up = (
                "bfd udp session set-flags admin up local-addr %s peer-addr %s "
                % (
                    self.src_ip_net,
                    self.dst_ip_net,
                )
            )
            self.cli_verify_response(
                cli_up,
                "bfd udp session set-flags: Incompatible parameter "
                "combination, interface must be set if not multihop",
            )

    def test_set_del_udp_echo_source(self):
        """set/del udp echo source"""

        if self.multihop:
            self.skipTest(
                f"Skipping because echo functionality is not supported with multihop"
            )

        self.create_loopback_interfaces(1)
        self.loopback0 = self.lo_interfaces[0]
        self.loopback0.admin_up()
        self.cli_verify_response("show bfd echo-source", "UDP echo source is not set.")
        cli_set = "bfd udp echo-source set interface %s" % self.loopback0.name
        self.cli_verify_no_response(cli_set)
        self.cli_verify_response(
            "show bfd echo-source",
            "UDP echo source is: %s\n"
            "IPv4 address usable as echo source: none\n"
            "IPv6 address usable as echo source: none" % self.loopback0.name,
        )
        self.loopback0.config_ip4()
        echo_ip4 = str(
            ipaddress.IPv4Address(
                int(ipaddress.IPv4Address(self.loopback0.local_ip4)) ^ 1
            )
        )
        self.cli_verify_response(
            "show bfd echo-source",
            "UDP echo source is: %s\n"
            "IPv4 address usable as echo source: %s\n"
            "IPv6 address usable as echo source: none"
            % (self.loopback0.name, echo_ip4),
        )
        echo_ip6 = str(
            ipaddress.IPv6Address(
                int(ipaddress.IPv6Address(self.loopback0.local_ip6)) ^ 1
            )
        )
        self.loopback0.config_ip6()
        self.cli_verify_response(
            "show bfd echo-source",
            "UDP echo source is: %s\n"
            "IPv4 address usable as echo source: %s\n"
            "IPv6 address usable as echo source: %s"
            % (self.loopback0.name, echo_ip4, echo_ip6),
        )
        cli_del = "bfd udp echo-source del"
        self.cli_verify_no_response(cli_del)
        self.cli_verify_response("show bfd echo-source", "UDP echo source is not set.")


if __name__ == "__main__":
    unittest.main(testRunner=VppTestRunner)
