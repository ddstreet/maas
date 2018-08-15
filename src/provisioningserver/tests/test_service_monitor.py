# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the DHCPv4 and DHCPv6 service driver."""

__all__ = []

from maastesting.testcase import (
    MAASTestCase,
    MAASTwistedRunTest,
)
from provisioningserver.rackdservices.testing import (
    prepareRegionForGetControllerType,
)
from provisioningserver.service_monitor import (
    DHCPv4Service,
    DHCPv6Service,
    DNSServiceOnRack,
    NTPServiceOnRack,
    service_monitor,
)
from provisioningserver.utils.service_monitor import SERVICE_STATE
from testtools.matchers import Equals
from twisted.internet.defer import inlineCallbacks


class TestDHCPv4Service(MAASTestCase):

    def test_name(self):
        service = DHCPv4Service()
        self.assertEqual("dhcpd", service.name)

    def test_service_name(self):
        service = DHCPv4Service()
        self.assertEqual("maas-dhcpd", service.service_name)


class TestDHCPv6Service(MAASTestCase):

    def test_name(self):
        service = DHCPv6Service()
        self.assertEqual("dhcpd6", service.name)

    def test_service_name(self):
        service = DHCPv6Service()
        self.assertEqual("maas-dhcpd6", service.service_name)


class TestNTPServiceOnRack(MAASTestCase):

    def test_name_and_service_name(self):
        ntp = NTPServiceOnRack()
        self.assertEqual("chrony", ntp.service_name)
        self.assertEqual("ntp_rack", ntp.name)


class TestNTPServiceOnRack_Scenarios(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    scenarios = (
        ("rack", dict(
            is_region=False, is_rack=True,
            expected_state=SERVICE_STATE.ON,
            expected_info=None,
        )),
        ("region", dict(
            is_region=True, is_rack=False,
            expected_state=SERVICE_STATE.ANY,
            expected_info=None,
        )),
        ("region+rack", dict(
            is_region=True, is_rack=True,
            expected_state=SERVICE_STATE.ANY,
            expected_info="managed by the region.",
        )),
        ("machine", dict(
            is_region=False, is_rack=False,
            expected_state=SERVICE_STATE.ANY,
            expected_info=None,
        )),
    )

    def setUp(self):
        super(TestNTPServiceOnRack_Scenarios, self).setUp()
        return prepareRegionForGetControllerType(
            self, is_region=self.is_region, is_rack=self.is_rack)

    @inlineCallbacks
    def test_getExpectedState(self):
        ntp = NTPServiceOnRack()
        self.assertThat(
            (yield ntp.getExpectedState()),
            Equals((self.expected_state, self.expected_info)))


class TestDNSServiceOnRack(MAASTestCase):

    def test_name_and_service_name(self):
        dns = DNSServiceOnRack()
        self.assertEqual("bind9", dns.service_name)
        self.assertEqual("dns_rack", dns.name)


class TestDNSServiceOnRack_Scenarios(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    scenarios = (
        ("rack", dict(
            is_region=False, is_rack=True,
            expected_state=SERVICE_STATE.ON,
            expected_info=None,
        )),
        ("region", dict(
            is_region=True, is_rack=False,
            expected_state=SERVICE_STATE.ANY,
            expected_info=None,
        )),
        ("region+rack", dict(
            is_region=True, is_rack=True,
            expected_state=SERVICE_STATE.ANY,
            expected_info="managed by the region.",
        )),
        ("machine", dict(
            is_region=False, is_rack=False,
            expected_state=SERVICE_STATE.ANY,
            expected_info=None,
        )),
    )

    def setUp(self):
        super(TestDNSServiceOnRack_Scenarios, self).setUp()
        return prepareRegionForGetControllerType(
            self, is_region=self.is_region, is_rack=self.is_rack)

    @inlineCallbacks
    def test_getExpectedState(self):
        dns = DNSServiceOnRack()
        self.assertThat(
            (yield dns.getExpectedState()),
            Equals((self.expected_state, self.expected_info)))


class TestGlobalServiceMonitor(MAASTestCase):

    def test__includes_all_services(self):
        self.assertItemsEqual(
            ["http", "dhcpd", "dhcpd6", "dns_rack", "ntp_rack", "proxy_rack"],
            service_monitor._services.keys())
