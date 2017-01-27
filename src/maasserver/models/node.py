# Copyright 2012-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Node objects."""

__all__ = [
    "Controller",
    "Device",
    "Node",
    "Machine",
    "RackController",
    "RegionController",
    ]

from collections import (
    defaultdict,
    namedtuple,
)
from datetime import timedelta
from functools import partial
from itertools import count
from operator import attrgetter
import random
import re
import socket
from socket import gethostname
from urllib.parse import urlparse

from crochet import TimeoutError
from django.contrib.auth.models import User
from django.core.exceptions import (
    PermissionDenied,
    ValidationError,
)
from django.db import connection
from django.db.models import (
    BigIntegerField,
    BooleanField,
    CASCADE,
    CharField,
    DateTimeField,
    ForeignKey,
    IntegerField,
    Manager,
    ManyToManyField,
    Model,
    OneToOneField,
    PositiveIntegerField,
    PROTECT,
    Q,
    SET_DEFAULT,
    SET_NULL,
    TextField,
)
from django.db.models.query import QuerySet
from django.shortcuts import get_object_or_404
from maasserver import DefaultMeta
from maasserver.clusterrpc.power import (
    power_cycle,
    power_driver_check,
    power_off_node,
    power_on_node,
    power_query,
    power_query_all,
)
from maasserver.enum import (
    ALLOCATED_NODE_STATUSES,
    FILESYSTEM_FORMAT_TYPE_CHOICES_DICT,
    FILESYSTEM_TYPE,
    INTERFACE_LINK_TYPE,
    INTERFACE_TYPE,
    IPADDRESS_FAMILY,
    IPADDRESS_TYPE,
    NODE_PERMISSION,
    NODE_STATUS,
    NODE_STATUS_CHOICES,
    NODE_STATUS_CHOICES_DICT,
    NODE_TYPE,
    NODE_TYPE_CHOICES,
    POWER_STATE,
    POWER_STATE_CHOICES,
    SERVICE_STATUS,
)
from maasserver.exceptions import (
    NodeStateViolation,
    PowerProblem,
)
from maasserver.fields import (
    JSONObjectField,
    MAASIPAddressField,
    MAC,
)
from maasserver.models.blockdevice import BlockDevice
from maasserver.models.bootresource import BootResource
from maasserver.models.cleansave import CleanSave
from maasserver.models.config import Config
from maasserver.models.domain import Domain
from maasserver.models.fabric import Fabric
from maasserver.models.filesystem import Filesystem
from maasserver.models.filesystemgroup import FilesystemGroup
from maasserver.models.interface import (
    BondInterface,
    BridgeInterface,
    Interface,
    PhysicalInterface,
    VLANInterface,
)
from maasserver.models.licensekey import LicenseKey
from maasserver.models.ownerdata import OwnerData
from maasserver.models.partitiontable import PartitionTable
from maasserver.models.physicalblockdevice import PhysicalBlockDevice
from maasserver.models.service import Service
from maasserver.models.staticipaddress import StaticIPAddress
from maasserver.models.subnet import Subnet
from maasserver.models.tag import Tag
from maasserver.models.timestampedmodel import (
    now,
    TimestampedModel,
)
from maasserver.models.vlan import VLAN
from maasserver.models.zone import Zone
from maasserver.node_status import (
    COMMISSIONING_LIKE_STATUSES,
    get_failed_status,
    is_failed_status,
    MONITORED_STATUSES,
    NODE_FAILURE_MONITORED_STATUS_TIMEOUTS,
    NODE_TRANSITIONS,
)
from maasserver.rpc import (
    getAllClients,
    getClientFor,
    getClientFromIdentifiers,
)
from maasserver.storage_layouts import (
    get_storage_layout_for_node,
    StorageLayoutError,
    StorageLayoutMissingBootDiskError,
)
from maasserver.utils.dns import validate_hostname
from maasserver.utils.mac import get_vendor_for_mac
from maasserver.utils.orm import (
    get_one,
    MAASQueriesMixin,
    post_commit,
    post_commit_do,
    transactional,
)
from maasserver.utils.threads import (
    callOutToDatabase,
    deferToDatabase,
)
from maasserver.worker_user import get_worker_user
from netaddr import (
    IPAddress,
    IPNetwork,
)
import petname
from piston3.models import Token
from provisioningserver.drivers.power.registry import PowerDriverRegistry
from provisioningserver.events import (
    EVENT_DETAILS,
    EVENT_TYPES,
)
from provisioningserver.logger import get_maas_logger
from provisioningserver.refresh import (
    get_sys_info,
    refresh,
)
from provisioningserver.rpc.cluster import (
    AddChassis,
    DisableAndShutoffRackd,
    IsImportBootImagesRunning,
    RefreshRackControllerInfo,
)
from provisioningserver.rpc.exceptions import (
    NoConnectionsAvailable,
    PowerActionFail,
    RefreshAlreadyInProgress,
    UnknownPowerType,
)
from provisioningserver.utils import (
    flatten,
    sorttop,
    znums,
)
from provisioningserver.utils.enum import map_enum_reverse
from provisioningserver.utils.env import (
    get_maas_id,
    set_maas_id,
)
from provisioningserver.utils.fs import NamedLock
from provisioningserver.utils.ipaddr import get_mac_addresses
from provisioningserver.utils.network import (
    annotate_with_default_monitored_interfaces,
)
from provisioningserver.utils.twisted import (
    asynchronous,
    callOut,
    deferWithTimeout,
)
from twisted.internet.defer import (
    Deferred,
    inlineCallbacks,
    succeed,
)
from twisted.internet.threads import deferToThread


maaslog = get_maas_logger("node")


# Holds the known `bios_boot_methods`. If `bios_boot_method` is not in this
# list then it will fallback to `DEFAULT_BIOS_BOOT_METHOD`.
KNOWN_BIOS_BOOT_METHODS = ["pxe", "uefi", "powernv", "powerkvm"]

# Default `bios_boot_method`. See `KNOWN_BIOS_BOOT_METHOD` above for usage.
DEFAULT_BIOS_BOOT_METHOD = "pxe"

# Return type from `get_effective_power_info`.
PowerInfo = namedtuple("PowerInfo", (
    "can_be_started",
    "can_be_stopped",
    "can_be_queried",
    "power_type",
    "power_parameters",
))

DefaultGateways = namedtuple("DefaultGateways", (
    "ipv4",
    "ipv6",
))

GatewayDefinition = namedtuple("GatewayDefinition", (
    "interface_id",
    "subnet_id",
    "gateway_ip",
))


def generate_node_system_id():
    """Return an unused six-digit system ID.

    This chooses an ID at random and returns it if it's not currently in use.
    There is a chance of a collision between concurrent processes, which would
    result in an `IntegrityError` in one process or the other, but it's small:
    there are over 183 million six-digit system IDs to choose from.
    """
    for attempt in range(1, 1001):
        system_num = random.randrange(24 ** 5, 24 ** 6)
        system_id = znums.from_int(system_num)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM maasserver_node "
                "WHERE system_id = %s", [system_id])
            if cursor.fetchone() is None:
                return system_id
    else:
        # Wow, really? This should _never_ happen. You must be managing a
        # *lot* of machines. This is here as a fail-safe; it does not feels
        # right to leave a loop that might never terminate in the code.
        raise AssertionError(
            "The unthinkable has come to pass: after %d iterations "
            "we could find no unused node identifiers." % attempt)


class NodeQueriesMixin(MAASQueriesMixin):

    def filter_by_spaces(self, spaces):
        """Return the set of nodes with at least one interface in the specified
        spaces.
        """
        return self.filter(
            interface__ip_addresses__subnet__vlan__space__in=spaces)

    def exclude_spaces(self, spaces):
        """Return the set of nodes without any interfaces in the specified
        spaces.
        """
        return self.exclude(
            interface__ip_addresses__subnet__vlan__space__in=spaces)

    def filter_by_fabrics(self, fabrics):
        """Return the set of nodes with at least one interface in the specified
        fabrics.
        """
        return self.filter(
            interface__vlan__fabric__in=fabrics)

    def exclude_fabrics(self, fabrics):
        """Return the set of nodes without any interfaces in the specified
        fabrics.
        """
        return self.exclude(
            interface__vlan__fabric__in=fabrics)

    def filter_by_fabric_classes(self, fabric_classes):
        """Return the set of nodes with at least one interface in the specified
        fabric classes.
        """
        return self.filter(
            interface__vlan__fabric__class_type__in=fabric_classes)

    def exclude_fabric_classes(
            self, fabric_classes):
        """Return the set of nodes without any interfaces in the specified
        fabric classes.
        """
        return self.exclude(
            interface__vlan__fabric__class_type__in=fabric_classes)

    def filter_by_vids(self, vids):
        """Return the set of nodes with at least one interface whose VLAN has
        one of the specified VIDs.
        """
        return self.filter(
            interface__vlan__vid__in=vids)

    def exclude_vids(self, vids):
        """Return the set of nodes without any interfaces whose VLAN has one of
        the specified VIDs.
        """
        return self.exclude(
            interface__vlan__vid__in=vids)

    def filter_by_subnets(self, subnets):
        """Return the set of nodes with at least one interface configured on
        one of the specified subnets.
        """
        return self.filter(
            interface__ip_addresses__subnet__in=subnets)

    def exclude_subnets(self, subnets):
        """Return the set of nodes without any interfaces configured on one of
        the specified subnets.
        """
        return self.exclude(
            interface__ip_addresses__subnet__in=subnets)

    def filter_by_subnet_cidrs(self, subnet_cidrs):
        """Return the set of nodes with at least one interface configured on
        one of the specified subnet with the given CIDRs.
        """
        return self.filter(
            interface__ip_addresses__subnet__cidr__in=subnet_cidrs)

    def exclude_subnet_cidrs(self, subnet_cidrs):
        """Return the set of nodes without any interfaces configured on one of
        the specified subnet with the given CIDRs.
        """
        return self.exclude(
            interface__ip_addresses__subnet__cidr__in=subnet_cidrs)

    def filter_by_domains(self, domain_names):
        """Return the set of nodes with at least one interface configured in
        one of the specified dns zone names.
        """
        return self.filter(
            interface__ip_addresses__dnsresource_set__domain__name__in=(
                domain_names))

    def exclude_domains(self, domain_names):
        """Return the set of nodes without any interfaces configured in
        one of the specified dns zone names.
        """
        return self.exclude(
            interface__ip_addresses__dnsresource_set__domain__name__in=(
                domain_names))


class NodeQuerySet(QuerySet, NodeQueriesMixin):
    """Custom QuerySet which mixes in some additional queries specific to
    nodes. This needs to be a mixin because an identical method is needed on
    both the Manager and all QuerySets which result from calling the manager.
    """


class BaseNodeManager(Manager, NodeQueriesMixin):
    """A utility to manage the collection of Nodes."""

    extra_filters = {}

    def get_queryset(self):
        queryset = NodeQuerySet(self.model, using=self._db)
        return queryset.filter(**self.extra_filters)

    def filter_by_ids(self, query, ids=None):
        """Filter `query` result set by system_id values.

        :param query: A QuerySet of Nodes.
        :type query: django.db.models.query.QuerySet_
        :param ids: Optional set of ids to filter by.  If given, nodes whose
            system_ids are not in `ids` will be ignored.
        :type param_ids: Sequence
        :return: A filtered version of `query`.

        .. _django.db.models.query.QuerySet: https://docs.djangoproject.com/
           en/dev/ref/models/querysets/

        """
        if ids is None:
            return query
        else:
            return query.filter(system_id__in=ids)

    def _filter_visible_nodes(self, nodes, user, perm):
        """Filter a `Node` query depending on user permissions.

        :param nodes: A `Node` query set.
        :param user: The user making the request; the filtering is based on
            their privileges.
        :param perm: Type of access requested.  For example, a user may be
            allowed to view some nodes that they are not allowed to edit.
        :type perm: `NODE_PERMISSION`
        :return: A version of `node` that is filtered to include only those
            nodes that `user` is allowed to access.
        """
        # If the data is corrupt, this can get called with None for
        # user where a Node should have an owner but doesn't.
        # Nonetheless, the code should not crash with corrupt data.
        if user is None:
            return nodes.none()
        if user.is_superuser:
            # Admin is allowed to see all nodes.
            return nodes
        # Non-admins aren't allowed to see controllers.
        nodes = nodes.exclude(
            Q(node_type__in=[
                NODE_TYPE.RACK_CONTROLLER,
                NODE_TYPE.REGION_CONTROLLER,
                NODE_TYPE.REGION_AND_RACK_CONTROLLER,
                ]))
        if perm == NODE_PERMISSION.VIEW:
            return nodes.filter(Q(owner__isnull=True) | Q(owner=user))
        elif perm == NODE_PERMISSION.EDIT:
            return nodes.filter(owner=user)
        elif perm == NODE_PERMISSION.ADMIN:
            return nodes.none()
        else:
            raise NotImplementedError(
                "Invalid permission check (invalid permission name: %s)." %
                perm)

    def get_nodes(self, user, perm, ids=None, from_nodes=None):
        """Fetch Nodes on which the User_ has the given permission.

        Warning: there could be a lot of nodes!  Keep scale in mind when
        calling this, and watch performance in general.  Prefetch related
        data where appropriate.

        :param user: The user that should be used in the permission check.
        :type user: User_
        :param perm: The permission to check.
        :type perm: a permission string from NODE_PERMISSION
        :param ids: If given, limit result to nodes with these system_ids.
        :type ids: Sequence.
        :param from_nodes: Optionally, restrict the answer to these nodes.
        :type from_nodes: Query set of `Node`.

        .. _User: https://
           docs.djangoproject.com/en/dev/topics/auth/
           #django.contrib.auth.models.User

        """
        if from_nodes is None:
            from_nodes = self.all()
        else:
            # Make sure even if given a query set of multiple node types
            # get_nodes only returns nodes applicable to this manager.
            from_nodes = from_nodes.filter(**self.extra_filters)
        nodes = self._filter_visible_nodes(from_nodes, user, perm)
        return self.filter_by_ids(nodes, ids)

    def get_node_or_404(self, system_id, user, perm, **kwargs):
        """Fetch a `Node` by system_id.  Raise exceptions if no `Node` with
        this system_id exist or if the provided user has not the required
        permission on this `Node`.

        :param name: The system_id.
        :type name: string
        :param user: The user that should be used in the permission check.
        :type user: django.contrib.auth.models.User
        :param perm: The permission to assert that the user has on the node.
        :type perm: unicode
        :raises: django.http.Http404_,
            :class:`maasserver.exceptions.PermissionDenied`.

        .. _django.http.Http404: https://
           docs.djangoproject.com/en/dev/topics/http/views/
           #the-http404-exception
        """
        kwargs.update(self.extra_filters)
        node = get_object_or_404(
            self.model, system_id=system_id, **kwargs)
        if user.has_perm(perm, node):
            return node.as_self()
        else:
            raise PermissionDenied()


class GeneralManager(BaseNodeManager):
    """All the node types:"""


class MachineManager(BaseNodeManager):
    """Machines (i.e. deployable objects)."""

    extra_filters = {'node_type': NODE_TYPE.MACHINE}

    def get_allocated_visible_machines(self, token, ids):
        """Fetch Machines that were allocated to the User_/oauth token.

        :param user: The user whose machines to fetch
        :type user: User_
        :param token: The OAuth token associated with the Machines.
        :type token: piston3.models.Token.
        :param ids: Optional set of IDs to filter by. If given, machines whose
            system_ids are not in `ids` will be ignored.
        :type param_ids: Sequence

        .. _User: https://
           docs.djangoproject.com/en/dev/topics/auth/
           #django.contrib.auth.models.User
        """
        if ids is None:
            machines = self.filter(token=token)
        else:
            machines = self.filter(token=token, system_id__in=ids)
        return machines

    def get_available_machines_for_acquisition(self, for_user):
        """Find the machines that can be acquired by the given user.

        :param for_user: The user who is to acquire the machine.
        :type for_user: :class:`django.contrib.auth.models.User`
        :return: Those machines which can be acquired by the user.
        :rtype: `django.db.models.query.QuerySet`
        """
        available_machines = self.get_nodes(for_user, NODE_PERMISSION.VIEW)
        return available_machines.filter(status=NODE_STATUS.READY)


class DeviceManager(BaseNodeManager):
    """Devices are all the non-deployable nodes."""

    extra_filters = {'node_type': NODE_TYPE.DEVICE}


class ControllerManager(BaseNodeManager):
    """All controllers `RackController`, `RegionController`, and
    `RegionRackController`."""

    extra_filters = {
        'node_type__in': [
            NODE_TYPE.RACK_CONTROLLER,
            NODE_TYPE.REGION_CONTROLLER,
            NODE_TYPE.REGION_AND_RACK_CONTROLLER,
            ]}


class RackControllerManager(ControllerManager):
    """Rack controllers are nodes which are used by MAAS to deploy nodes."""

    extra_filters = {
        'node_type__in': [
            NODE_TYPE.RACK_CONTROLLER,
            NODE_TYPE.REGION_AND_RACK_CONTROLLER,
            ]}

    def get_running_controller(self):
        """Return the rack controller for the current host.

        :raises: `DoesNotExist` if no matching controller is found.
        """
        return self.get(system_id=get_maas_id())

    def filter_by_url_accessible(self, url, with_connection=True):
        """Return a list of rack controllers which have access to the given URL

        If a hostname is given MAAS will do a DNS lookup to discover the IP(s).
        MAAS then uses the information it has about the network to return a
        a list of rack controllers which should have access to each IP."""
        if '://' not in url:
            # urlparse only works if given with a protocol
            parsed_url = urlparse("FAKE://%s" % url)
        else:
            parsed_url = urlparse(url)
        # getaddrinfo can return duplicates
        ips = set(
            address[4][0]
            for address in socket.getaddrinfo(parsed_url.hostname, None)
        )
        subnets = set(Subnet.objects.get_best_subnet_for_ip(ip) for ip in ips)
        usable_racks = set(RackController.objects.filter(
            interface__ip_addresses__subnet__in=subnets,
            interface__ip_addresses__ip__isnull=False))
        # There is no MAAS defined subnet for loop back so if its in our list
        # of IPs add ourself
        if '127.0.0.1' in ips or '::1' in ips:
            running_rack = self.get_running_controller()
            if running_rack is not None:
                usable_racks.add(running_rack)

        if with_connection:
            conn_rack_ids = [client.ident for client in getAllClients()]
            return [
                rack
                for rack in usable_racks
                if rack.system_id in conn_rack_ids
            ]
        else:
            return list(usable_racks)

    def get_accessible_by_url(self, url, with_connection=True):
        """Return a rack controller with access to a URL."""
        racks = self.filter_by_url_accessible(url, with_connection)
        if not racks:
            return None
        else:
            # Lazy load balancing
            return random.choice(racks)


class RegionControllerManager(ControllerManager):
    """Region controllers are the API, UI, and Coordinators of MAAS."""

    extra_filters = {
        'node_type__in': [
            NODE_TYPE.REGION_CONTROLLER,
            NODE_TYPE.REGION_AND_RACK_CONTROLLER,
            ]}

    def get_running_controller(self):
        """Return the region controller for the current host.

        :raises: `DoesNotExist` if no matching controller is found.
        """
        return self.get(system_id=get_maas_id())

    def get_or_create_running_controller(self):
        """Return the region controller for the current host.

        :attention: This should be called early in the start-up process for a
            region controller to ensure that it can refer to itself.

        If the MAAS ID has been set, this searches for the controller only by
        its system ID. If the controller is not found `DoesNotExist` will be
        raised.

        If the MAAS ID has not yet been set this tries to discover a matching
        node via the current host's name and its MAC addresses. If no matching
        node is found, a new region controller will be created. In either case
        the MAAS ID will be set on the filesystem once the transaction has
        been committed.
        """
        maas_id = get_maas_id()
        if maas_id is None:
            node = self._find_or_create_running_controller()
        else:
            try:
                node = Node.objects.get(system_id=maas_id)
            except Node.DoesNotExist:
                # The MAAS ID on the filesystem is stale. Perhaps this machine
                # has not been cleaned-up sufficiently from a previous life?
                # Regardless, a deliberate act has brought this about, like a
                # purge of the database, so we continue instead of crashing.
                node = self._find_or_create_running_controller()
            else:
                node = self._upgrade_running_node(node)

        # A region needs to have a commissioning_script_set available to
        # allow commissioning data to be sent on start.
        if node.current_commissioning_script_set is None:
            # Avoid circular dependencies
            from metadataserver.models import ScriptSet

            script_set = ScriptSet.objects.create_commissioning_script_set(
                node)
            node.current_commissioning_script_set = script_set
            node.save()

        return node

    def _find_or_create_running_controller(self):
        """Find the controller for the current host, or create one.

        Tries to discover the controller via the current host's name and MAC
        addresses. Don't use this if the MAAS ID has been set.

        If the discovered node is not yet a controller it is upgraded. If no
        node is found a preconfigured controller is minted. In either case,
        the MAAS ID on the filesystem is set post-commit.
        """
        node = self._find_running_node()
        if node is None:
            region = self._create_running_controller()
        else:
            region = self._upgrade_running_node(node)
        post_commit_do(set_maas_id, region.system_id)
        return region

    def _find_running_node(self):
        """Find the node for the current host.

        Tries to discover the node via the current host's name and MAC
        addresses. Don't use this if the MAAS ID has been set.
        """
        hostname = gethostname()
        filter_hostname = Q(hostname=hostname)
        filter_macs = Q(interface__mac_address__in=get_mac_addresses())
        # Look at all nodes, not just controllers; we might have to upgrade.
        nodes = Node.objects.filter(filter_hostname | filter_macs)
        # Select distinct because the join to MACs might yield duplicates.
        return get_one(nodes.distinct())

    def _upgrade_running_node(self, node):
        """Upgrade a node to a region controller for the host machine.

        This node is already known to MAAS, but this MAY be the first time
        that regiond has run on it, so ensure it's a region, owned by the
        worker user.
        """
        update_fields = []
        if not node.is_region_controller:
            if node.is_rack_controller:
                node.node_type = NODE_TYPE.REGION_AND_RACK_CONTROLLER
            else:
                node.node_type = NODE_TYPE.REGION_CONTROLLER
            update_fields.append("node_type")
        if node.owner is None:
            node.owner = get_worker_user()
            update_fields.append("owner")
        if len(update_fields) > 0:
            node.save(update_fields=update_fields)
        # Always cast to a region controller.
        return node.as_region_controller()

    def _create_running_controller(self):
        """Create a region controller for the host machine.

        This node is NOT previously known to MAAS, and this is the first time
        regiond has run on it. Create a region controller only; it can be
        upgraded to a region+rack controller later if necessary.
        """
        hostname = gethostname()
        # Bug#1614584: it is possible that gethostname() reurns the FQDN.
        # Split it up, and get the appropriate domain.  If we wind up creating
        # one for it, we are not authoritative.
        # Just in case the default domain has not been created, let's create it
        # here, even if we subsequently overwrite it inside the if statement.
        domain = Domain.objects.get_default_domain()
        if hostname.find('.') > 0:
            hostname, domainname = hostname.split('.', 1)
            (domain, _) = Domain.objects.get_or_create(
                name=domainname, defaults={'authoritative': False})
        return self.create(
            owner=get_worker_user(), hostname=hostname, domain=domain)


def get_default_domain():
    """Get the default domain name."""
    return Domain.objects.get_default_domain().id


def get_default_zone():
    """Return the ID of the default zone."""
    return Zone.objects.get_default_zone().id


# List of statuses for which it makes sense to release a node.
RELEASABLE_STATUSES = [
    NODE_STATUS.ALLOCATED,
    NODE_STATUS.RESERVED,
    NODE_STATUS.BROKEN,
    NODE_STATUS.DEPLOYING,
    NODE_STATUS.DEPLOYED,
    NODE_STATUS.FAILED_DEPLOYMENT,
    NODE_STATUS.FAILED_DISK_ERASING,
    NODE_STATUS.FAILED_RELEASING,
    ]


class Node(CleanSave, TimestampedModel):
    """A `Node` represents a physical machine used by the MAAS Server.

    :ivar system_id: The unique identifier for this `Node`.
        (e.g. 'node-41eba45e-4cfa-11e1-a052-00225f89f211').
    :ivar hostname: This `Node`'s hostname.  Must conform to RFCs 952 and 1123.
    :ivar node_type: The type of node. This is used to specify if the node is
        to be used as a node for deployment, as a device, or a rack controller
    :ivar parent: An optional parent `Node`.  This node will be deleted along
        with all its resources when the parent node gets deleted or released.
        This is only relevant for node types other than node.
    :ivar status: This `Node`'s status. See the vocabulary
        :class:`NODE_STATUS`.
    :ivar previous_status: This `Node`'s previous status.  See the vocabulary
        :class:`NODE_STATUS`.
    :ivar error_description: A human-readable description of why a node is
        marked broken.  Only meaningful when the node is in the state 'BROKEN'.
    :ivar owner: This `Node`'s owner if it's in use, None otherwise.
    :ivar bios_boot_method: The boot method used by the cluster to allow
        this node to boot. E.g. "pxe".
    :ivar osystem: This `Node`'s booting operating system, if it's blank then
        the default_osystem will be used.
    :ivar distro_series: This `Node`'s booting distro series, if
        it's blank then the default_distro_series will be used.
    :ivar bmc: The BMC / power controller for this node.
    :ivar tags: The list of :class:`Tag`s associated with this `Node`.
    :ivar objects: The :class:`GeneralManager`.
    :ivar enable_ssh: An optional flag to indicate if this node can have
        ssh enabled during commissioning, allowing the user to ssh into the
        machine's commissioning environment using the user's SSH key.
    :ivar skip_networking: An optional flag to indicate if this node
        networking configuration doesn't need to be touched when it is
        commissioned.
    :ivar default_user: The username this `Node` will be configured with,
        None otherwise.
    """

    class Meta(DefaultMeta):
        """Needed for South to recognize this model."""

    system_id = CharField(
        max_length=41, unique=True, default=generate_node_system_id,
        editable=False)

    hostname = CharField(
        max_length=255, default='', blank=True, unique=True,
        validators=[validate_hostname])

    # What Domain do we use for this host unless the individual StaticIPAddress
    # record overrides it?
    domain = ForeignKey(
        Domain, default=get_default_domain, null=True, blank=True,
        editable=True, on_delete=PROTECT)

    # TTL for this Node's IP addresses.  Since this must be the same for all
    # records of the same time on any given name, we need to coordinate the TTL
    # with any addresses that come from DNSResource.
    # If None, then we inherit from the parent Domain, or the global default.
    address_ttl = PositiveIntegerField(default=None, null=True, blank=True)

    status = IntegerField(
        choices=NODE_STATUS_CHOICES, editable=False,
        default=NODE_STATUS.DEFAULT)

    previous_status = IntegerField(
        choices=NODE_STATUS_CHOICES, editable=False,
        default=NODE_STATUS.DEFAULT)

    # Set to time in the future when the node status should transition to
    # a failed status. This is used by the StatusMonitorService inside
    # the region processes. Each run periodically to update nodes.
    status_expires = DateTimeField(
        null=True, blank=False, default=None, editable=False)

    owner = ForeignKey(
        User, default=None, blank=True, null=True, editable=False,
        on_delete=PROTECT)

    bios_boot_method = CharField(max_length=31, blank=True, null=True)

    osystem = CharField(
        max_length=255, blank=True, default='')

    distro_series = CharField(
        max_length=255, blank=True, default='')

    architecture = CharField(max_length=31, blank=True, null=True)

    min_hwe_kernel = CharField(max_length=31, blank=True, null=True)

    hwe_kernel = CharField(max_length=31, blank=True, null=True)

    node_type = IntegerField(
        choices=NODE_TYPE_CHOICES, editable=False, default=NODE_TYPE.DEFAULT)

    parent = ForeignKey(
        "Node", default=None, blank=True, null=True, editable=True,
        related_name="children", on_delete=CASCADE)

    agent_name = CharField(max_length=255, default='', blank=True, null=True)

    error_description = TextField(blank=True, default='', editable=False)

    zone = ForeignKey(
        Zone, verbose_name="Physical zone",
        default=get_default_zone, editable=True, db_index=True,
        on_delete=SET_DEFAULT)

    # Juju expects the following standard constraints, which are stored here
    # as a basic optimisation over querying the lshw output.
    cpu_count = IntegerField(default=0)
    cpu_speed = IntegerField(default=0)  # MHz
    memory = IntegerField(default=0)

    swap_size = BigIntegerField(null=True, blank=True, default=None)

    bmc = ForeignKey(
        'BMC', db_index=True, null=True, editable=False, unique=False)

    # Power parameters specific to this node instance. Global power parameters
    # are stored in this node's BMC.
    instance_power_parameters = JSONObjectField(
        max_length=(2 ** 15), blank=True, default="")

    power_state = CharField(
        max_length=10, null=False, blank=False,
        choices=POWER_STATE_CHOICES, default=POWER_STATE.UNKNOWN,
        editable=False)

    # Set when a rack controller says its going to update the power state
    # for this node. This prevents other rack controllers from also checking
    # this node at the same time.
    power_state_queried = DateTimeField(
        null=True, blank=False, default=None, editable=False)

    # Set when a rack controller has actually checked this power state and
    # the last time the power was updated.
    power_state_updated = DateTimeField(
        null=True, blank=False, default=None, editable=False)

    # Updated each time a rack controller finishes syncing boot images.
    last_image_sync = DateTimeField(
        null=True, blank=False, default=None, editable=False)

    token = ForeignKey(
        Token, db_index=True, null=True, editable=False, unique=False)

    error = CharField(max_length=255, blank=True, default='')

    netboot = BooleanField(default=True)

    license_key = CharField(max_length=30, null=True, blank=True)

    # Only used by Machine. Set to True when the machine was composed
    # dynamically from a pod during allocation. When the machine is
    # released it will be deleted.
    dynamic = BooleanField(default=False)

    tags = ManyToManyField(Tag)

    # Record the Interface the node last booted from.
    # This will be used for determining which Interface to create a static
    # IP reservation for when starting a node.
    boot_interface = ForeignKey(
        Interface, default=None, blank=True, null=True, editable=False,
        related_name='+', on_delete=SET_NULL)

    # Record the last IP address of the cluster this node used to request
    # TFTP data. This is used to send the correct IP address for the node to
    # download the image to install. Since the node just contacted the cluster
    # using this IP address then it will be able to access the images at this
    # IP address.
    boot_cluster_ip = MAASIPAddressField(
        unique=False, null=True, editable=False, blank=True, default=None)

    # Record the PhysicalBlockDevice that this node uses as its boot disk.
    # This will be used to make sure GRUB is installed to this device.
    boot_disk = ForeignKey(
        PhysicalBlockDevice, default=None, blank=True, null=True,
        editable=False, related_name='+', on_delete=SET_NULL)

    # Default IPv4 subnet link on an interface for this node. This is used to
    # define the default IPv4 route the node should use.
    gateway_link_ipv4 = ForeignKey(
        StaticIPAddress, default=None, blank=True, null=True,
        editable=False, related_name='+', on_delete=SET_NULL)

    # Default IPv6 subnet link on an interface for this node. This is used to
    # define the default IPv6 route the node should use.
    gateway_link_ipv6 = ForeignKey(
        StaticIPAddress, default=None, blank=True, null=True,
        editable=False, related_name='+', on_delete=SET_NULL)

    # Used to configure the default username for this machine. It will be
    # empty by default, and the default user.
    default_user = CharField(
        max_length=32, blank=True, default='')

    # Used to determine whether to:
    #  1. Import the SSH Key during commissioning and keep power on.
    #  2. Skip reconfiguring networking when a node is commissioned.
    #  3. Skip reconfiguring storage when a node is commissioned.
    enable_ssh = BooleanField(default=False)
    skip_networking = BooleanField(default=False)
    skip_storage = BooleanField(default=False)

    # The URL the RackController uses to access to RegionController's.
    url = CharField(
        blank=True, editable=False, max_length=255, default='')

    # Used only by a RegionController to determine which
    # RegionControllerProcess is currently controlling DNS on this node.
    # Used only by `REGION_CONTROLLER` all other types this should be NULL.
    dns_process = OneToOneField(
        "RegionControllerProcess", null=True, editable=False, unique=True,
        on_delete=SET_NULL, related_name="+")

    # Used only by a RackController to mark which RegionControllerProcess is
    # handling system level events for this rack controller.
    managing_process = ForeignKey(
        "RegionControllerProcess", null=True, editable=False,
        on_delete=SET_NULL, related_name="+")

    # The ScriptSet for the currently running, or last run, commissioning
    # ScriptSet.
    current_commissioning_script_set = ForeignKey(
        "metadataserver.ScriptSet", blank=True, null=True, on_delete=SET_NULL,
        related_name="+")

    # The ScriptSet for the currently running, or last run, installation.
    current_installation_script_set = ForeignKey(
        "metadataserver.ScriptSet", blank=True, null=True, on_delete=SET_NULL,
        related_name="+")

    # The ScriptSet for the currently running, or last run, test ScriptSet.
    current_testing_script_set = ForeignKey(
        "metadataserver.ScriptSet", blank=True, null=True, on_delete=SET_NULL,
        related_name="+")

    # Note that the ordering of the managers is meaningful.  More precisely,
    # the first manager defined is important: see
    # https://docs.djangoproject.com/en/1.7/topics/db/managers/ ("Default
    # managers") for details.
    # 'objects' are all the nodes types
    objects = GeneralManager()

    # Manager that returns all controller objects. See `ControllerManager`.
    controllers = ControllerManager()

    def __str__(self):
        if self.hostname:
            return "%s (%s)" % (self.system_id, self.fqdn)
        else:
            return self.system_id

    @property
    def disable_ipv4(self):
        return False

    @property
    def is_rack_controller(self):
        return self.node_type in [
            NODE_TYPE.REGION_AND_RACK_CONTROLLER,
            NODE_TYPE.RACK_CONTROLLER,
            ]

    @property
    def is_region_controller(self):
        return self.node_type in [
            NODE_TYPE.REGION_AND_RACK_CONTROLLER,
            NODE_TYPE.REGION_CONTROLLER,
            ]

    @property
    def is_controller(self):
        return self.node_type in [
            NODE_TYPE.REGION_CONTROLLER,
            NODE_TYPE.REGION_AND_RACK_CONTROLLER,
            NODE_TYPE.RACK_CONTROLLER,
            ]

    @property
    def is_machine(self):
        return self.node_type == NODE_TYPE.MACHINE

    @property
    def is_device(self):
        return self.node_type == NODE_TYPE.DEVICE

    @property
    def power_type(self):
        return '' if self.bmc is None else self.bmc.power_type

    @power_type.setter
    def power_type(self, power_type):
        # Circular imports.
        from maasserver.models.bmc import BMC
        if not power_type and self.bmc is None:
            return
        if self.bmc is not None and self.bmc.power_type == power_type:
            return
        self.bmc, _ = BMC.objects.get_or_create(
            power_type=power_type, power_parameters=self.power_parameters)

    @property
    def power_parameters(self):
        # Overlay instance power parameters over bmc power parameters.
        instance_parameters = self.instance_power_parameters
        if not instance_parameters:
            instance_parameters = {}
        bmc_parameters = {}
        if self.bmc and self.bmc.power_parameters:
            bmc_parameters = self.bmc.power_parameters
        return {**bmc_parameters, **instance_parameters}

    @power_parameters.setter
    def power_parameters(self, power_params):
        if not power_params:
            power_params = {}
        if self.bmc is None:
            self.instance_power_parameters = power_params
            return

        # Circular imports.
        from maasserver.models.bmc import BMC
        bmc_params, node_params = BMC.scope_power_parameters(
            self.bmc.power_type, power_params)
        self.instance_power_parameters = node_params

        if self.bmc.power_parameters == bmc_params:
            return

        conflicts = len(BMC.objects.filter(
            power_type=self.bmc.power_type, power_parameters=bmc_params)) > 0
        if not conflicts:
            self.bmc.power_parameters = bmc_params
            self.bmc.save()
        else:
            (bmc, _) = BMC.objects.get_or_create(
                power_type=self.bmc.power_type, power_parameters=bmc_params)
            # Point all nodes using old BMC at the new one.
            if self.bmc is not None and self.bmc_id != bmc.id:
                for node in self.bmc.node_set.exclude(id=self.id):
                    node.bmc = bmc
                    node.save()
            self.bmc = bmc

    @property
    def fqdn(self):
        """Fully qualified domain name for this node.

        Return the FQDN for this host.
        """
        if self.domain is not None:
            return '%s.%s' % (self.hostname, self.domain.name)
        else:
            return self.hostname

    def get_deployment_time(self):
        """Return the deployment time of this node (in seconds).

        This is the maximum time the deployment is allowed to take.
        """
        # Return a *very* conservative estimate for now.
        # Something that shouldn't conflict with any deployment.
        return timedelta(
            minutes=NODE_FAILURE_MONITORED_STATUS_TIMEOUTS[
                NODE_STATUS.DEPLOYING]).total_seconds()

    def get_commissioning_time(self):
        """Return the commissioning time of this node (in seconds).

        This is the maximum time the commissioning is allowed to take.
        """
        # Return a *very* conservative estimate for now.
        return timedelta(
            minutes=NODE_FAILURE_MONITORED_STATUS_TIMEOUTS[
                NODE_STATUS.COMMISSIONING]).total_seconds()

    def get_entering_rescue_mode_time(self):
        """Return the entering-rescue-mode time of this node (in seconds).

        This is the maximum time the entering-rescue-mode is allowed to take.
        """
        # Return a *very* conservative estimate for now.
        return timedelta(
            minutes=NODE_FAILURE_MONITORED_STATUS_TIMEOUTS[
                NODE_STATUS.ENTERING_RESCUE_MODE]).total_seconds()

    def get_releasing_time(self):
        """Return the releasing time of this node (in seconds).

        This is the maximum time that releasing is allowed to take.
        """
        return timedelta(
            minutes=NODE_FAILURE_MONITORED_STATUS_TIMEOUTS[
                NODE_STATUS.RELEASING]).total_seconds()

    def _register_request_event(
            self, user, type_name, action='', comment=None):
        """Register a node request event.

        It registers events like start_commission (started by a user),
        or mark_failed (started by the system)"""

        # the description will be the comment, if any.
        description = comment if comment else ''
        # if the user exists, we need to construct the description with
        # the user. as it would be a user-driven request.
        if user is not None:
            if len(description) == 0:
                description = "(%s)" % user
            else:
                description = "(%s) - %s" % (user, description)
        event_details = EVENT_DETAILS[type_name]

        # Avoid circular imports.
        from maasserver.models.event import Event
        Event.objects.register_event_and_event_type(
            self.system_id, type_name, type_level=event_details.level,
            type_description=event_details.description,
            event_action=action,
            event_description=description)

    def storage_layout_issues(self):
        """Return any errors with the storage layout.

        Checks that the node has / mounted. If / is mounted on bcache check
        that /boot is mounted and is not on bcache."""
        def on_bcache(obj):
            if obj.type == "physical":
                return False
            elif obj.type == "partition":
                return on_bcache(obj.partition_table.block_device)
            for parent in obj.virtualblockdevice.get_parents():
                if((parent.type != "physical" and on_bcache(parent)) or
                    (parent.get_effective_filesystem().fstype in
                     [FILESYSTEM_TYPE.BCACHE_CACHE,
                      FILESYSTEM_TYPE.BCACHE_BACKING])):
                    return True
            return False

        has_boot = False
        root_mounted = False
        root_on_bcache = False
        boot_mounted = False
        arch, subarch = self.split_arch()

        for block_device in self.blockdevice_set.all():
            if block_device.is_boot_disk():
                has_boot = True
            pt = block_device.get_partitiontable()
            if pt is not None:
                for partition in pt.partitions.all():
                    fs = partition.get_effective_filesystem()
                    if fs is None:
                        continue
                    if fs.mount_point == '/':
                        root_mounted = True
                        if on_bcache(block_device):
                            root_on_bcache = True
                    elif (fs.mount_point == '/boot' and
                          not on_bcache(block_device)):
                        boot_mounted = True
            else:
                fs = block_device.get_effective_filesystem()
                if fs is None:
                    continue
                if fs.mount_point == '/':
                    root_mounted = True
                    if on_bcache(block_device):
                        root_on_bcache = True
                elif fs.mount_point == '/boot' and not on_bcache(block_device):
                    boot_mounted = True
        issues = []
        if not has_boot:
            issues.append(
                "Specify a storage device to be able to deploy this node.")
        if not root_mounted:
            issues.append(
                "Mount the root '/' filesystem to be able to deploy this "
                "node.")
        if root_mounted and root_on_bcache and not boot_mounted:
            issues.append(
                "This node cannot be deployed because it cannot boot from a "
                "bcache volume. Mount /boot on a non-bcache device to be "
                "able to deploy this node.")
        if (not boot_mounted and arch == "arm64" and
                self.get_bios_boot_method() != "uefi"):
            issues.append(
                "This node cannot be deployed because it needs a separate "
                "/boot partition.  Mount /boot on a device to be able to "
                "deploy this node.")
        return issues

    def on_network(self):
        """Return true if the node is connected to a managed network."""
        for interface in self.interface_set.all():
            for link in interface.get_links():
                if (link['mode'] != INTERFACE_LINK_TYPE.LINK_UP and
                        'subnet' in link):
                    return True
        return False

    def _start_deployment(self):
        """Mark a node as being deployed."""
        # Avoid circular dependencies
        from metadataserver.models import ScriptSet
        if self.on_network() is False:
            raise ValidationError(
                {"network":
                 ["Node must be configured to use a network"]})
        storage_layout_issues = self.storage_layout_issues()
        if len(storage_layout_issues) > 0:
            raise ValidationError({"storage": storage_layout_issues})
        self.status = NODE_STATUS.DEPLOYING
        script_set = ScriptSet.objects.create_installation_script_set(self)
        self.current_installation_script_set = script_set
        self.save()

    def end_deployment(self):
        """Mark a node as successfully deployed."""
        self.status = NODE_STATUS.DEPLOYED
        self.save()

    def ip_addresses(self):
        """IP addresses allocated to this node.

        Return the current IP addresses for this Node, or the empty
        list if there are none.
        """
        # If the node has static IP addresses assigned they will be returned
        # before the dynamic IP addresses are returned. The dynamic IP
        # addresses will only be returned if the node has no static IP
        # addresses.
        ips = self.static_ip_addresses()
        if len(ips) == 0:
            ips = self.dynamic_ip_addresses()
        return ips

    def static_ip_addresses(self):
        """Static IP addresses allocated to this node."""
        # DHCP is included here because it is a configured type. Its not
        # just set randomly by the lease parser.
        ip_addresses = StaticIPAddress.objects.filter(
            interface__node=self, ip__isnull=False,
            alloc_type__in=[
                IPADDRESS_TYPE.DHCP,
                IPADDRESS_TYPE.AUTO,
                IPADDRESS_TYPE.STICKY,
                IPADDRESS_TYPE.USER_RESERVED,
            ])
        ips = []
        for ip in ip_addresses:
            ip = ip.get_ip()
            if ip:
                ips.append(ip)
        return ips

    def dynamic_ip_addresses(self):
        """Dynamic IP addresses allocated to this node."""
        ip_addresses = StaticIPAddress.objects.filter(
            interface__node=self, alloc_type=IPADDRESS_TYPE.DISCOVERED)
        return [
            ip
            for ip in ip_addresses.values_list('ip', flat=True)
            if ip
        ]

    def get_interface_names(self):
        return list(self.interface_set.all().values_list('name', flat=True))

    def get_next_ifname(self, ifnames=None):
        """
        Scans the interfaces on this Node and returns the next free ifname in
        the format 'ethX', where X is zero or a positive integer.
        """
        if ifnames is None:
            ifnames = self.get_interface_names()
        used_ethX = []
        for ifname in ifnames:
            match = re.match('eth([0-9]+)', ifname)
            if match is not None:
                ifnum = int(match.group(1))
                used_ethX.append(ifnum)
        if len(used_ethX) == 0:
            return "eth0"
        else:
            ifnum = max(used_ethX) + 1
            return "eth%d" % ifnum

    def get_block_device_names(self):
        return list(self.blockdevice_set.all().values_list('name', flat=True))

    def get_next_block_device_name(self, block_device_names=None, prefix='sd'):
        """
        Scans the block devices on this Node and returns the next free block
        device name in the format '{prefix}X', where X is [a-z]+.
        """
        if block_device_names is None:
            block_device_names = self.get_block_device_names()
        for idx in count(0):
            name = BlockDevice._get_block_name_from_idx(idx, prefix=prefix)
            if name not in block_device_names:
                return name

    def tag_names(self):
        # We don't use self.tags.values_list here because this does not
        # take advantage of the cache.
        return [tag.name for tag in self.tags.all()]

    def clean_boot_disk(self, prev):
        """Check that the boot disk is either un-used or has a partition
        table.

        It's possible that the boot disk we are seeing is already in-use with a
        filesystem group or cache set.
        """
        if self.boot_disk is not None:
            filesystem = self.boot_disk.get_effective_filesystem()
            if filesystem is not None:
                if filesystem.fstype in FILESYSTEM_FORMAT_TYPE_CHOICES_DICT:
                    # Format-able filesystem so it can just be removed.
                    filesystem.delete()
                elif filesystem.filesystem_group is not None:
                    # Part of a filesystem group and cannot be set as the
                    # boot disk.
                    raise ValidationError({
                        "boot_disk": [
                            "Cannot be set as the boot disk; already in-use "
                            "in %s '%s'." % (
                                filesystem.filesystem_group.get_nice_name(),
                                filesystem.filesystem_group.name,
                                )]
                        })
                elif filesystem.cache_set is not None:
                    # Part of a cache set and cannot be set as the boot disk.
                    raise ValidationError({
                        "boot_disk": [
                            "Cannot be set as the boot disk; already in-use "
                            "in cache set '%s'." % (
                                filesystem.cache_set.name,
                                )]
                        })

    def clean_boot_interface(self, prev):
        """Check that this Node's boot interface (if present) belongs to this
        Node.

        It's possible, though very unlikely, that the boot interface we are
        seeing is already assigned to another Node. If this happens, we need to
        catch the failure as early as possible.
        """
        if (self.boot_interface is not None and self.id is not None and
                self.id != self.boot_interface.node_id):
                raise ValidationError({
                    'boot_interface': [
                        "Must be one of the node's interfaces."],
                    })

    def clean_status(self, prev):
        """Check a node's status transition against the node-status FSM."""
        old_status = None if prev is None else prev.status
        if self.status == old_status:
            # No transition is always a safe transition.
            pass
        elif old_status is None:
            # No transition to check as it has no previous status.
            pass
        elif self.status in NODE_TRANSITIONS.get(old_status, ()):
            # Valid transition.
            stat = map_enum_reverse(NODE_STATUS, ignore=['DEFAULT'])
            maaslog.info(
                "%s: Status transition from %s to %s",
                self.hostname, stat[old_status], stat[self.status])
        else:
            # Transition not permitted.
            error_text = "Invalid transition: %s -> %s." % (
                NODE_STATUS_CHOICES_DICT.get(old_status, "Unknown"),
                NODE_STATUS_CHOICES_DICT.get(self.status, "Unknown"),
                )
            raise NodeStateViolation(error_text)

    def clean_architecture(self, prev):
        if self.architecture == '':
            raise ValidationError(
                {'architecture':
                    ["Architecture must be defined for installable nodes."]})

    def clean_hostname_domain(self, prev):
        # If you set the hostname to a name with dots, that you mean for that
        # to be the FQDN of the host. Se we check that a domain exists for
        # the remaining portion of the hostname.
        if self.hostname.find('.') > -1:
            # They have specified an FQDN.  Split up the pieces, and throw
            # an error if the domain does not exist.
            name, domainname = self.hostname.split('.', 1)
            domains = Domain.objects.filter(name=domainname)
            if domains.count() == 1:
                self.hostname = name
                self.domain = domains[0]
            else:
                raise ValidationError(
                    {'hostname': ["Nonexistant domain."]})
        elif self.domain is None:
            self.domain = Domain.objects.get_default_domain()

    def clean(self, *args, **kwargs):
        super(Node, self).clean(*args, **kwargs)
        prev = get_one(Node.objects.filter(pk=self.pk))
        self.prev_bmc_id = prev.bmc_id if prev else None
        self.clean_hostname_domain(prev)
        self.clean_status(prev)
        self.clean_architecture(prev)
        self.clean_boot_disk(prev)
        self.clean_boot_interface(prev)

    def remove_orphaned_bmcs(self):
        # If bmc has changed post-save, clean up any potentially orphaned BMC.
        if self.prev_bmc_id is not None and self.prev_bmc_id != self.bmc_id:
            # Circular imports.
            from maasserver.models.bmc import BMC
            try:
                used_bmcs = [id for id in Node.objects.values_list(
                    'bmc_id', flat=True).distinct() if id is not None]
                BMC.objects.exclude(id__in=used_bmcs).delete()
            except Exception as error:
                maaslog.info(
                    "%s: Failure cleaning orphaned BMC's: %s",
                    self.hostname, error)

    def save(self, *args, **kwargs):
        # Reset the status_expires if not a monitored status. This prevents
        # a race condition seen in LP1603563 where an old status_expires caused
        # the node to do in a FAILED_RELEASING state due to an old
        # status_expire being set.
        if self.status not in MONITORED_STATUSES:
            self.status_expires = None

        super(Node, self).save(*args, **kwargs)

        # We let hostname be blank for the initial save, but fix it before the
        # save completes.  This is because set_random_hostname() operates by
        # trying to re-save the node with a random hostname, and retrying until
        # there is no conflict.  The end result is that no IP addresses will
        # ever be linked to any node that has a blank hostname, since the node
        # must be saved for there to be any linkage to it from an interface.
        if self.hostname == '':
            self.set_random_hostname()
        self.remove_orphaned_bmcs()

    def display_status(self):
        """Return status text as displayed to the user."""
        return NODE_STATUS_CHOICES_DICT[self.status]

    def display_memory(self):
        """Return memory in GiB."""
        if self.memory < 1024:
            return '%.1f' % (self.memory / 1024.0)
        # Commissioning gets all available memory to the system. However some
        # memory can be reserved by the motherboard(e.g for video memory) or
        # the kernel itself. Commissioning can't detect reserved RAM so show
        # the RAM in GiB to the first decimal place. Python rounds the float
        # which results in the correct value. For example a KVM virt is
        # configured with 2048 MiB of RAM but only 2047MiB is detectable.
        # 2047 / 1024 = 1.9990 which rounds to 2.0.
        return '%.1f' % (self.memory / 1024.0)

    @property
    def physicalblockdevice_set(self):
        """Return `QuerySet` for all `PhysicalBlockDevice` assigned to node.

        This is need as Django doesn't add this attribute to the `Node` model,
        it only adds blockdevice_set.
        """
        return PhysicalBlockDevice.objects.filter(node=self)

    @property
    def virtualblockdevice_set(self):
        """Return `QuerySet` for all `VirtualBlockDevice` assigned to node.

        This is need as Django doesn't add this attribute to the `Node` model,
        it only adds blockdevice_set.
        """
        # Avoid circular imports.
        from maasserver.models.virtualblockdevice import VirtualBlockDevice
        return VirtualBlockDevice.objects.filter(node=self)

    @property
    def storage(self):
        """Return storage in megabytes.

        Compatility with API 1.0 this field needs to exist on the Node.
        """
        size = (
            PhysicalBlockDevice.objects.total_size_of_physical_devices_for(
                self))
        return size / 1000 / 1000

    def display_storage(self):
        """Return storage in gigabytes."""
        if self.storage < 1000:
            return '%.1f' % (self.storage / 1000.0)
        return '%d' % (self.storage / 1000)

    def get_boot_disk(self):
        """Return the boot disk for this node."""
        if self.boot_disk is not None:
            return self.boot_disk
        else:
            # Fallback to using the first created physical block device as
            # the boot disk.
            return self.physicalblockdevice_set.order_by('id').first()

    def get_bios_boot_method(self):
        """Return the boot method the node's BIOS booted."""
        if self.bios_boot_method not in KNOWN_BIOS_BOOT_METHODS:
            if self.bios_boot_method:
                maaslog.warning(
                    "%s: Has a unknown BIOS boot method '%s'; "
                    "defaulting to '%s'." % (
                        self.hostname,
                        self.bios_boot_method,
                        DEFAULT_BIOS_BOOT_METHOD,
                        ))
            return DEFAULT_BIOS_BOOT_METHOD
        else:
            return self.bios_boot_method

    def add_physical_interface(self, mac_address, name=None):
        """Add a new `PhysicalInterface` to `node` with `mac_address`."""
        # Avoid circular imports.
        from maasserver.models import PhysicalInterface, UnknownInterface
        if name is None:
            name = self.get_next_ifname()
        mac = MAC(mac_address)
        UnknownInterface.objects.filter(mac_address=mac).delete()
        try:
            iface = PhysicalInterface.objects.get(mac_address=mac)
        except PhysicalInterface.DoesNotExist:
            return PhysicalInterface.objects.create(
                node=self, mac_address=mac, name=name)
        if iface.node != self:
            # This MAC address is already registered to a different node.
            raise ValidationError(
                "MAC address %s already in use on %s." % (
                    mac_address, iface.node.hostname))
        return iface

    def accept_enlistment(self, user):
        """Accept this node's (anonymous) enlistment.

        This call makes sense only on a node in New state, i.e. one that
        has been anonymously enlisted and is now waiting for a MAAS user to
        accept that enlistment as authentic.  Calling it on a node that is in
        Ready or Commissioning state, however, is not an error -- it probably
        just means that somebody else has beaten you to it.

        :return: This node if it has made the transition from New, or
            None if it was already in an accepted state.
        """
        accepted_states = [NODE_STATUS.READY, NODE_STATUS.COMMISSIONING]
        if self.status in accepted_states:
            return None
        if self.status != NODE_STATUS.NEW:
            raise NodeStateViolation(
                "Cannot accept node enlistment: node %s is in state %s."
                % (self.system_id, NODE_STATUS_CHOICES_DICT[self.status]))

        self.start_commissioning(user)
        return self

    @classmethod
    @transactional
    def _set_status_expires(self, system_id, seconds):
        """Set the status_expires field on node."""
        try:
            node = Node.objects.get(system_id=system_id)
        except Node.DoesNotExist:
            return

        db_time = now()
        node.status_expires = db_time + timedelta(seconds=seconds)
        node.save(update_fields=['status_expires'])

    @classmethod
    @transactional
    def _clear_status_expires(self, system_id):
        """Clear the status_expires field on node."""
        try:
            node = Node.objects.get(system_id=system_id)
        except Node.DoesNotExist:
            return

        node.status_expires = None
        node.save(update_fields=['status_expires'])

    @transactional
    def start_commissioning(
            self, user, enable_ssh=False, skip_networking=False,
            skip_storage=False):
        """Install OS and self-test a new node.

        :return: a `Deferred` which contains the post-commit tasks that are
            required to run to start and commission the node. This is already
            registered as a post-commit hook; it should not be added a second
            time.
        """
        # Avoid circular imports.
        from metadataserver.models import ScriptSet
        from metadataserver.user_data.commissioning import generate_user_data

        # Only commission if power type is configured.
        if self.power_type == '':
            raise UnknownPowerType(
                "Unconfigured power type. "
                "Please configure the power type and try again.")

        self._register_request_event(
            user, EVENT_TYPES.REQUEST_NODE_START_COMMISSIONING,
            action='start commissioning')

        # Set the commissioning options on the node.
        self.enable_ssh = enable_ssh
        self.skip_networking = skip_networking
        self.skip_storage = skip_storage

        # Generate the specific user data for commissioning this node.
        commissioning_user_data = generate_user_data(node=self)

        # Create a new ScriptSet for this commissioning run.
        script_set = ScriptSet.objects.create_commissioning_script_set(self)
        self.current_commissioning_script_set = script_set

        # Clear the current storage configuration if networking is not being
        # skipped during commissioning.
        if not self.skip_storage:
            self._clear_full_storage_configuration()

        # Clear the current network configuration if networking is not being
        # skipped during commissioning.
        if not self.skip_networking:
            self._clear_networking_configuration()

        # We need to mark the node as COMMISSIONING now to avoid a race
        # when starting multiple nodes. We hang on to old_status just in
        # case the power action fails.
        old_status = self.status
        self.status = NODE_STATUS.COMMISSIONING
        self.owner = user
        # Set min_hwe_kernel to min_hwe_kernel if it isn't set incase
        # commissioning failed and the user set the min globally and is
        # retrying.
        if not self.min_hwe_kernel:
            self.min_hwe_kernel = Config.objects.get_config(
                'default_min_hwe_kernel')
        self.save()

        try:
            # Node.start() has synchronous and asynchronous parts, so catch
            # exceptions arising synchronously, and chain callbacks to the
            # Deferred it returns for the asynchronous (post-commit) bits.
            starting = self._start(
                user, commissioning_user_data, old_status,
                allow_power_cycle=True)
        except Exception as error:
            self.status = old_status
            self.save()
            maaslog.error(
                "%s: Could not start node for commissioning: %s",
                self.hostname, error)
            # Let the exception bubble up, since the UI or API will have to
            # deal with it.
            raise
        else:
            # Don't permit naive mocking of start(); it causes too much
            # confusion when testing. Return a Deferred from side_effect.
            assert isinstance(starting, Deferred) or starting is None

            post_commit().addCallback(
                callOutToDatabase, Node._set_status_expires,
                self.system_id, self.get_commissioning_time())

            if starting is None:
                starting = post_commit()
                # MAAS cannot start the node itself.
                is_starting = False
            else:
                # MAAS can direct the node to start.
                is_starting = True

            starting.addCallback(
                callOut, self._start_commissioning_async, is_starting,
                self.hostname)

            def eb_start(failure, hostname):
                maaslog.error(
                    "%s: Could not start node for commissioning: %s",
                    hostname, failure.getErrorMessage())
                return failure  # Propagate.

            return starting.addErrback(eb_start, self.hostname)

    @classmethod
    @asynchronous
    def _start_commissioning_async(cls, is_starting, hostname):
        """Start commissioning, the post-commit bits.

        :param is_starting: A boolean indicating if MAAS is able to start this
            node itself, or if manual intervention is needed.
        :param hostname: The node's hostname, for logging.
        """
        if is_starting:
            maaslog.info("%s: Commissioning started", hostname)
        else:
            maaslog.warning(
                "%s: Could not start node for commissioning; it "
                "must be started manually", hostname)

    @transactional
    def abort_commissioning(self, user, comment=None):
        """Power off a commissioning node and set its status to NEW.

        :return: a `Deferred` which contains the post-commit tasks that are
            required to run to stop the node. This is already registered as a
            post-commit hook; it should not be added a second time.
        """
        if self.status != NODE_STATUS.COMMISSIONING:
            raise NodeStateViolation(
                "Cannot abort commissioning of a non-commissioning node: "
                "node %s is in state %s."
                % (self.system_id, NODE_STATUS_CHOICES_DICT[self.status]))

        self._register_request_event(
            user, EVENT_TYPES.REQUEST_NODE_ABORT_COMMISSIONING,
            action='abort commissioning', comment=comment)

        try:
            # Node.stop() has synchronous and asynchronous parts, so catch
            # exceptions arising synchronously, and chain callbacks to the
            # Deferred it returns for the asynchronous (post-commit) bits.
            stopping = self._stop(user)
            if self.owner is not None:
                self.owner = None
                self.save()
        except Exception as error:
            maaslog.error(
                "%s: Error when aborting commissioning: %s",
                self.hostname, error)
            raise
        else:
            # Don't permit naive mocking of stop(); it causes too much
            # confusion when testing. Return a Deferred from side_effect.
            assert isinstance(stopping, Deferred) or stopping is None

            post_commit().addCallback(
                callOutToDatabase, Node._clear_status_expires, self.system_id)

            if stopping is None:
                stopping = post_commit()
                # MAAS cannot stop the node itself.
                is_stopping = False
            else:
                # MAAS can direct the node to stop.
                is_stopping = True

            stopping.addCallback(
                callOut, self._abort_commissioning_async, is_stopping,
                self.hostname, self.system_id)

            def eb_abort(failure, hostname):
                maaslog.error(
                    "%s: Error when aborting commissioning: %s",
                    hostname, failure.getErrorMessage())
                return failure  # Propagate.

            return stopping.addErrback(eb_abort, self.hostname)

    @transactional
    def abort_deploying(self, user, comment=None):
        """Power off a deploying node and set its status to ALLOCATED.

        :return: a `Deferred` which contains the post-commit tasks that are
            required to run to stop the node. This is already registered as a
            post-commit hook; it should not be added a second time.
        """
        if self.status != NODE_STATUS.DEPLOYING:
            raise NodeStateViolation(
                "Cannot abort deployment of a non-deploying node: "
                "node %s is in state %s."
                % (self.system_id, NODE_STATUS_CHOICES_DICT[self.status]))

        self._register_request_event(
            user, EVENT_TYPES.REQUEST_NODE_ABORT_DEPLOYMENT,
            action='abort deploying', comment=comment)

        try:
            # Node.stop() has synchronous and asynchronous parts, so catch
            # exceptions arising synchronously, and chain callbacks to the
            # Deferred it returns for the asynchronous (post-commit) bits.
            stopping = self._stop(user)
        except Exception as error:
            maaslog.error(
                "%s: Error when aborting deployment: %s",
                self.hostname, error)
            raise
        else:
            # Don't permit naive mocking of stop(); it causes too much
            # confusion when testing. Return a Deferred from side_effect.
            assert isinstance(stopping, Deferred) or stopping is None

            post_commit().addCallback(
                callOutToDatabase, Node._clear_status_expires, self.system_id)

            if stopping is None:
                stopping = post_commit()
                # MAAS cannot stop the node itself.
                is_stopping = False
            else:
                # MAAS can direct the node to stop.
                is_stopping = True

            stopping.addCallback(
                callOut, self._abort_deploying_async, is_stopping,
                self.hostname, self.system_id)

            def eb_abort(failure, hostname):
                maaslog.error(
                    "%s: Error when aborting deployment: %s",
                    hostname, failure.getErrorMessage())
                return failure  # Propagate.

            return stopping.addErrback(eb_abort, self.hostname)

    @classmethod
    @asynchronous
    def _abort_commissioning_async(cls, is_stopping, hostname, system_id):
        """Abort commissioning, the post-commit bits.

        :param is_stopping: A boolean indicating if MAAS is able to stop this
            node itself, or if manual intervention is needed.
        :param hostname: The node's hostname, for logging.
        :param system_id: The system ID for the node.
        """
        d = deferToDatabase(cls._set_status, system_id, status=NODE_STATUS.NEW)
        if is_stopping:
            return d.addCallback(
                callOut, maaslog.info, "%s: Commissioning aborted", hostname)
        else:
            return d.addCallback(
                callOut, maaslog.warning, "%s: Could not stop node to abort "
                "commissioning; it must be stopped manually", hostname)

    @classmethod
    @asynchronous
    def _abort_deploying_async(cls, is_stopping, hostname, system_id):
        """Abort deploying, the post-commit bits.

        :param is_stopping: A boolean indicating if MAAS is able to stop this
            node itself, or if manual intervention is needed.
        :param hostname: The node's hostname, for logging.
        :param system_id: The system ID for the node.
        """
        d = deferToDatabase(
            cls._set_status, system_id, status=NODE_STATUS.ALLOCATED)
        if is_stopping:
            return d.addCallback(
                callOut, maaslog.info, "%s: Deployment aborted", hostname)
        else:
            return d.addCallback(
                callOut, maaslog.warning, "%s: Could not stop node to abort "
                "deployment; it must be stopped manually", hostname)

    def delete(self):
        """Delete this node."""
        maaslog.info("%s: Deleting node", self.hostname)

        # Delete the related interfaces. This will remove all of IP addresses
        # that are linked to those interfaces.
        self.interface_set.all().delete()

        # Delete my BMC if no other Nodes are using it.
        if self.bmc is not None and self.bmc.node_set.count() == 1 and (
                self.bmc.node_set.first() == self):
            # Delete my orphaned BMC.
            maaslog.info(
                "%s: Deleting my BMC '%s'", self.hostname, self.bmc)
            self.bmc.delete()

        super(Node, self).delete()

    def set_random_hostname(self):
        """Set a random `hostname`."""
        while True:
            self.hostname = petname.Generate(2, "-")
            try:
                self.save()
            except ValidationError:
                pass
            else:
                break

    def get_effective_power_type(self):
        """Get power-type to use for this node.

        If no power type has been set for the node, raise
        UnknownPowerType.
        """
        if self.bmc is None or self.bmc.power_type == '':
            raise UnknownPowerType("Node power type is unconfigured")
        return self.bmc.power_type

    def get_effective_kernel_options(self):
        """Determine any special kernel parameters for this node.

        :return: (tag, kernel_options)
            tag is a Tag object or None. If None, the kernel_options came from
            the global setting.
            kernel_options, a string indicating extra kernel_options that
            should be used when booting this node. May be None if no tags match
            and no global setting has been configured.
        """
        # First, see if there are any tags associated with this node that has a
        # custom kernel parameter
        tags = self.tags.filter(kernel_opts__isnull=False)
        tags = tags.order_by('name')
        for tag in tags:
            if tag.kernel_opts != '':
                return tag, tag.kernel_opts
        global_value = Config.objects.get_config('kernel_opts')
        return None, global_value

    def get_osystem(self, default=None):
        """Return the operating system to install that node."""
        use_default_osystem = (self.osystem is None or self.osystem == '')
        if use_default_osystem:
            if default is None:
                default = Config.objects.get_config('default_osystem')
            return default
        else:
            return self.osystem

    def get_distro_series(self, default=None):
        """Return the distro series to install that node."""
        use_default_osystem = (
            self.osystem is None or
            self.osystem == '')
        use_default_distro_series = (
            self.distro_series is None or
            self.distro_series == '')
        if use_default_osystem and use_default_distro_series:
            if default is None:
                default = Config.objects.get_config('default_distro_series')
            return default
        else:
            return self.distro_series

    def get_effective_license_key(self):
        """Return effective license key.

        This returns the license key that should be used during the
        installation of the operating system for this node. This method first
        checks to see if the node has a specific license key set, if not then
        the license key registry is checked, if neither exists for this node or
        the booting operating system and release combination then an empty
        string is returned. An empty string can mean two things, one the
        operating system does not require a license key, or the installation
        media already has the license key builtin.
        """
        use_global_license_key = (
            self.license_key is None or
            self.license_key == '')
        if use_global_license_key:
            osystem = self.get_osystem()
            distro_series = self.get_distro_series()
            try:
                return LicenseKey.objects.get_license_key(
                    osystem, distro_series)
            except LicenseKey.DoesNotExist:
                return ''
        else:
            return self.license_key

    def get_effective_power_parameters(self):
        """Return effective power parameters, including any defaults."""
        power_params = self.power_parameters.copy()

        power_params.setdefault('system_id', self.system_id)
        # TODO: We should not be sending these paths to the templates;
        # the templates ought to know which tool to use themselves.
        power_params.setdefault('fence_cdu', '/usr/sbin/fence_cdu')
        power_params.setdefault('ipmipower', '/usr/sbin/ipmipower')
        power_params.setdefault('ipmitool', '/usr/bin/ipmitool')
        power_params.setdefault(
            'ipmi_chassis_config', '/usr/sbin/ipmi-chassis-config')
        power_params.setdefault('ipmi_config', 'ipmi.conf')
        # TODO: /end of paths that templates should know.
        # TODO: This default ought to be in the virsh template.
        if self.bmc is not None and self.bmc.power_type == "virsh":
            power_params.setdefault(
                'power_address', 'qemu://localhost/system')
        else:
            power_params.setdefault('power_address', "")
        power_params.setdefault('username', '')
        power_params.setdefault('power_id', self.system_id)
        power_params.setdefault('power_driver', '')
        power_params.setdefault('power_pass', '')
        power_params.setdefault('power_off_mode', '')

        # The "mac" parameter defaults to the node's boot interace MAC
        # address, but only if not already set.
        if 'mac_address' not in power_params:
            boot_interface = self.get_boot_interface()
            if boot_interface is not None:
                mac = boot_interface.mac_address.get_raw()
                power_params['mac_address'] = mac

        # boot_mode is something that tells the template whether this is
        # a PXE boot or a local HD boot.
        if (self.status == NODE_STATUS.DEPLOYED or
                self.node_type != NODE_TYPE.MACHINE):
            power_params['boot_mode'] = 'local'
        else:
            power_params['boot_mode'] = 'pxe'

        return power_params

    def get_effective_power_info(self):
        """Get information on how to control this node's power.

        Returns a ``(can-be-started, can-be-stopped, power-type,
        power-parameters)`` tuple, where ``can-be-started`` and
        ``can-be-stopped`` are hints, based on the power type and power
        parameters, whether it's even worth trying to control this node's
        power.

        Put another way, if ``can-be-started`` is `False`, the node almost
        certainly cannot be started. If it's `True`, then it may be possible
        to control this node's power, but there are *no* guarantees. The same
        goes for ``can-be-stopped``.

        :return: :py:class:`PowerInfo` (a `namedtuple`)
        """
        power_params = self.get_effective_power_parameters()
        try:
            power_type = self.get_effective_power_type()
        except UnknownPowerType:
            maaslog.warning("%s: Unrecognised power type.", self.hostname)
            return PowerInfo(False, False, False, None, None)
        else:
            if power_type == 'manual' or self.node_type in (
                    NODE_TYPE.REGION_CONTROLLER,
                    NODE_TYPE.REGION_AND_RACK_CONTROLLER):
                can_be_started = False
                can_be_stopped = False
            else:
                can_be_started = True
                can_be_stopped = True
            power_driver = PowerDriverRegistry.get_item(power_type)
            if power_driver is not None:
                can_be_queried = power_driver.queryable
            else:
                can_be_queried = False
            return PowerInfo(
                can_be_started, can_be_stopped, can_be_queried,
                power_type, power_params,
            )

    @staticmethod
    @asynchronous
    @inlineCallbacks
    def confirm_power_driver_operable(client, power_type, conn_ident):
        missing_packages = yield power_driver_check(client, power_type)
        if len(missing_packages) > 0:
            missing_packages = sorted(missing_packages)
            if len(missing_packages) > 2:
                missing_packages = [", ".join(
                    missing_packages[:-1]), missing_packages[-1]]
            package_list = " and ".join(missing_packages)
            raise PowerActionFail(
                "Power control software is missing from the rack "
                "controller %s. To proceed, "
                "install the %s package%s." % (
                    conn_ident,
                    package_list,
                    "s" if len(missing_packages) > 1 else ""))

    def acquire(
            self, user, token=None, agent_name='', comment=None,
            bridge_all=False, bridge_stp=None, bridge_fd=None):
        """Mark commissioned node as acquired by the given user and token."""
        assert self.owner is None
        assert token is None or token.user == user

        self._create_acquired_filesystems()
        self._register_request_event(
            user, EVENT_TYPES.REQUEST_NODE_ACQUIRE, action='acquire',
            comment=comment)
        self.status = NODE_STATUS.ALLOCATED
        self.owner = user
        self.agent_name = agent_name
        self.token = token
        if bridge_all:
            self._create_acquired_bridges(
                bridge_stp=bridge_stp, bridge_fd=bridge_fd)
        self.save()
        maaslog.info("%s: allocated to user %s", self.hostname, user.username)

    def set_zone(self, zone):
        """Set this node's zone"""
        old_zone_name = self.zone.name
        self.zone = zone
        self.save()
        maaslog.info("%s: moved from %s zone to %s zone." % (
            self.hostname, old_zone_name, self.zone.name))

    def start_disk_erasing(
            self, user, comment=None, secure_erase=None, quick_erase=None):
        """Erase the disks on a node.

        :return: a `Deferred` which contains the post-commit tasks that are
            required to run to start and erase the node. This is already
            registered as a post-commit hook; it should not be added a second
            time.
        """
        # Avoid circular imports.
        from metadataserver.user_data.disk_erasing import generate_user_data

        # Generate the user data based on the global options and the passed
        # configuration.
        use_secure_erase = Config.objects.get_config(
            'disk_erase_with_secure_erase')
        use_quick_erase = Config.objects.get_config(
            'disk_erase_with_quick_erase')
        if secure_erase is not None:
            use_secure_erase = secure_erase
        if quick_erase is not None:
            use_quick_erase = quick_erase
        disk_erase_user_data = generate_user_data(
            node=self,
            secure_erase=use_secure_erase,
            quick_erase=use_quick_erase)

        self._register_request_event(
            user, EVENT_TYPES.REQUEST_NODE_ERASE_DISK,
            action='start disk erasing', comment=comment)

        # Change the status of the node now to avoid races when starting
        # nodes in bulk.
        old_status = self.status
        self.status = NODE_STATUS.DISK_ERASING
        self.save()

        try:
            # Node.start() has synchronous and asynchronous parts, so catch
            # exceptions arising synchronously, and chain callbacks to the
            # Deferred it returns for the asynchronous (post-commit) bits.
            starting = self._start(
                user, disk_erase_user_data, old_status, allow_power_cycle=True)
        except Exception as error:
            # We always mark the node as failed here, although we could
            # potentially move it back to the state it was in previously. For
            # now, though, this is safer, since it marks the node as needing
            # attention.
            self.status = NODE_STATUS.FAILED_DISK_ERASING
            self.save()
            maaslog.error(
                "%s: Could not start node for disk erasure: %s",
                self.hostname, error)
            # Let the exception bubble up, since the UI or API will have to
            # deal with it.
            raise
        else:
            # Don't permit naive mocking of start(); it causes too much
            # confusion when testing. Return a Deferred from side_effect.
            assert isinstance(starting, Deferred) or starting is None

            if starting is None:
                starting = post_commit()
                # MAAS cannot start the node itself.
                is_starting = False
            else:
                # MAAS can direct the node to start.
                is_starting = True

            starting.addCallback(
                callOut, self._start_disk_erasing_async, is_starting,
                self.hostname)

            # If there's an error, reset the node's status.
            starting.addErrback(
                callOutToDatabase, Node._set_status, self.system_id,
                status=NODE_STATUS.FAILED_DISK_ERASING)

            def eb_start(failure, hostname):
                maaslog.error(
                    "%s: Could not start node for disk erasure: %s",
                    hostname, failure.getErrorMessage())
                return failure  # Propagate.

            return starting.addErrback(eb_start, self.hostname)

    @classmethod
    @asynchronous
    def _start_disk_erasing_async(cls, is_starting, hostname):
        """Start disk erasing, some of the post-commit bits.

        :param is_starting: A boolean indicating if MAAS is able to start this
            node itself, or if manual intervention is needed.
        :param hostname: The node's hostname, for logging.
        """
        if is_starting:
            maaslog.info("%s: Disk erasure started", hostname)
        else:
            maaslog.warning(
                "%s: Could not start node for disk erasure; it "
                "must be started manually", hostname)

    def abort_disk_erasing(self, user, comment=None):
        """Power off disk erasing node and set a failed status.

        :return: a `Deferred` which contains the post-commit tasks that are
            required to run to stop the node. This is already registered as a
            post-commit hook; it should not be added a second time.
        """
        if self.status != NODE_STATUS.DISK_ERASING:
            raise NodeStateViolation(
                "Cannot abort disk erasing of a non disk erasing node: "
                "node %s is in state %s."
                % (self.system_id, NODE_STATUS_CHOICES_DICT[self.status]))

        self._register_request_event(
            user, EVENT_TYPES.REQUEST_NODE_ABORT_ERASE_DISK,
            action='abort disk erasing', comment=comment)

        try:
            # Node.stop() has synchronous and asynchronous parts, so catch
            # exceptions arising synchronously, and chain callbacks to the
            # Deferred it returns for the asynchronous (post-commit) bits.
            stopping = self._stop(user)
        except Exception as error:
            maaslog.error(
                "%s: Error when aborting disk erasure: %s",
                self.hostname, error)
            raise
        else:
            # Don't permit naive mocking of stop(); it causes too much
            # confusion when testing. Return a Deferred from side_effect.
            assert isinstance(stopping, Deferred) or stopping is None

            if stopping is None:
                stopping = post_commit()
                # MAAS cannot stop the node itself.
                is_stopping = False
            else:
                # MAAS can direct the node to stop.
                is_stopping = True

            stopping.addCallback(
                callOut, self._abort_disk_erasing_async, is_stopping,
                self.hostname, self.system_id)

            def eb_abort(failure, hostname):
                maaslog.error(
                    "%s: Error when aborting disk erasure: %s",
                    hostname, failure.getErrorMessage())
                return failure  # Propagate.

            return stopping.addErrback(eb_abort, self.hostname)

    @classmethod
    @asynchronous
    def _abort_disk_erasing_async(cls, is_stopping, hostname, system_id):
        """Abort disk erasing, some of the post-commit bits.

        :param is_stopping: A boolean indicating if MAAS is able to stop this
            node itself, or if manual intervention is needed.
        :param hostname: The node's hostname, for logging.
        :param system_id: The system ID for the node.
        """
        d = deferToDatabase(
            cls._set_status, system_id, status=NODE_STATUS.FAILED_DISK_ERASING)
        if is_stopping:
            return d.addCallback(
                callOut, maaslog.info, "%s: Disk erasing aborted", hostname)
        else:
            return d.addCallback(
                callOut, maaslog.warning, "%s: Could not stop node to abort "
                "disk erasure; it must be stopped manually", hostname)

    def abort_operation(self, user, comment=None):
        """Abort the current operation.
        """
        if self.status == NODE_STATUS.DISK_ERASING:
            self.abort_disk_erasing(user, comment)
            return
        if self.status == NODE_STATUS.COMMISSIONING:
            self.abort_commissioning(user, comment)
            return
        if self.status == NODE_STATUS.DEPLOYING:
            self.abort_deploying(user, comment)
            return
        raise NodeStateViolation(
            "Cannot abort in current state: "
            "node %s is in state %s."
            % (self.system_id, NODE_STATUS_CHOICES_DICT[self.status]))

    def release(self, user=None, comment=None):
        self._register_request_event(
            user, EVENT_TYPES.REQUEST_NODE_RELEASE, action='release',
            comment=comment)
        self._release(user)

    def _release(self, user=None):
        """Mark allocated or reserved node as available again and power off.
        """
        maaslog.info("%s: Releasing node", self.hostname)

        # Don't perform stop the node if its already off. Doing so will
        # place an action in the power registry which is not needed and can
        # block a following deploy action. See bug 1453954 for an example of
        # the issue this will cause.
        if self.power_state != POWER_STATE.OFF:
            try:
                # Node.stop() has synchronous and asynchronous parts, so catch
                # exceptions arising synchronously, and chain callbacks to the
                # Deferred it returns for the asynchronous (post-commit) bits.
                stopping = self._stop(self.owner)
                # If there's an error, reset the node's status.
                # Check for None (_stop returns None for manual power type).
                if stopping is not None:
                    stopping.addErrback(
                        callOutToDatabase, Node._set_status, self.system_id,
                        status=self.status)
            except Exception as ex:
                maaslog.error(
                    "%s: Unable to shut node down: %s", self.hostname,
                    str(ex))
                raise

        if self.power_state == POWER_STATE.OFF:
            # The node is already powered off; we can deallocate all attached
            # resources and mark the node READY without delay.
            finalize_release = True
        elif self.get_effective_power_info().can_be_queried:
            # Controlled power type (one for which we can query the power
            # state): update_power_state() will take care of making the node
            # READY, remove the owner, and release the assigned auto IP
            # addresses when the power is finally off.
            post_commit().addCallback(
                callOutToDatabase, Node._set_status_expires,
                self.system_id, self.get_releasing_time())
            finalize_release = False
        else:
            # The node's power cannot be reliably controlled. Frankly, this
            # node is not suitable for use with MAAS. Deallocate all attached
            # resources and mark the node READY without delay because there's
            # not much else we can do.
            finalize_release = True

        self.status = NODE_STATUS.RELEASING
        self.token = None
        self.agent_name = ''
        self.set_netboot()
        self.osystem = ''
        self.distro_series = ''
        self.license_key = ''
        self.hwe_kernel = None
        self.save()

        # Clear installation results
        if self.current_installation_script_set is not None:
            self.current_installation_script_set.delete()
            self.current_installation_script_set = None

        # Clear the nodes acquired filesystems.
        self._clear_acquired_filesystems()

        # If this node has non-installable children, remove them.
        self.children.all().delete()

        # Power was off or cannot be powered off so release to ready now.
        if finalize_release:
            self._finalize_release()

    @transactional
    def _finalize_release(self):
        """Release all remaining resources, mark the machine `READY` if not
        dynamic, otherwise delete the machine.

        Releasing a node can be straightforward or it can be a multi-step
        operation, which can include a reboot in order to erase disks, then a
        final power-down. This method should be the absolute last method
        called.
        """
        if self.dynamic:
            self.delete()
        else:
            self.release_interface_config()
            self.status = NODE_STATUS.READY
            self.owner = None
            self.save()

            # Remove all set owner data.
            OwnerData.objects.filter(node=self).delete()

    def release_or_erase(
            self, user, comment=None,
            erase=False, secure_erase=None, quick_erase=None):
        """Either release the node or erase the node then release it, depending
        on settings and parameters."""
        erase_on_release = Config.objects.get_config(
            'enable_disk_erasing_on_release')
        if erase or erase_on_release:
            self.start_disk_erasing(
                user, comment,
                secure_erase=secure_erase, quick_erase=quick_erase)
        else:
            self.release(user, comment)

    def set_netboot(self, on=True):
        """Set netboot on or off."""
        maaslog.debug("%s: Turning on netboot for node", self.hostname)
        self.netboot = on
        self.save()

    def split_arch(self):
        """Return architecture and subarchitecture, as a tuple."""
        if self.architecture is None:
            return ("", "")
        arch, subarch = self.architecture.split('/')
        return (arch, subarch)

    def mark_failed(self, user=None, comment=None, commit=True):
        """Mark this node as failed.

        The actual 'failed' state depends on the current status of the
        node.
        """
        if user is None:
            # This is a system-driven event. Log level is ERROR.
            event_type = EVENT_TYPES.REQUEST_NODE_MARK_FAILED_SYSTEM
        else:
            # This is a user-driven event. Log level is INFO.
            event_type = EVENT_TYPES.REQUEST_NODE_MARK_FAILED
        self._register_request_event(
            user, event_type, action='mark_failed',
            comment=comment)

        new_status = get_failed_status(self.status)
        if new_status is not None:
            self.status = new_status
            self.error_description = comment if comment else ''
            if commit:
                self.save()
            maaslog.error(
                "%s: Marking node failed: %s", self.hostname, comment)
        elif self.status == NODE_STATUS.NEW:
            # Silently ignore, failing a new node makes no sense.
            pass
        elif is_failed_status(self.status):
            # Silently ignore a request to fail an already failed node.
            pass
        else:
            raise NodeStateViolation(
                "The status of the node is %s; this status cannot "
                "be transitioned to a corresponding failed status." %
                self.status)

    def mark_broken(self, user, comment=None):
        """Mark this node as 'BROKEN'.

        If the node is allocated, release it first.
        """
        self._register_request_event(
            user, EVENT_TYPES.REQUEST_NODE_MARK_BROKEN, action='mark broken',
            comment=comment)
        if self.status in RELEASABLE_STATUSES:
            self._release(user)
        # release() normally sets the status to RELEASING and leaves the
        # owner in place, override that here as we're broken.
        self.status = NODE_STATUS.BROKEN
        self.owner = None
        self.error_description = comment if comment else ''
        self.save()

    def mark_fixed(self, user, comment=None):
        """Mark a broken node as fixed and change its state to 'READY'."""
        self._register_request_event(
            user, EVENT_TYPES.REQUEST_NODE_MARK_FIXED, action='mark fixed',
            comment=comment)
        if self.status != NODE_STATUS.BROKEN:
            raise NodeStateViolation(
                "Can't mark a non-broken node as 'Ready'.")
        maaslog.info("%s: Marking node fixed", self.hostname)
        self.status = NODE_STATUS.READY
        self.error_description = ''
        self.osystem = ''
        self.distro_series = ''
        self.hwe_kernel = ''
        self.save()

        # Clear installation results
        if self.current_installation_script_set is not None:
            self.current_installation_script_set.delete()

    @transactional
    def update_power_state(self, power_state):
        """Update a node's power state """
        self.power_state = power_state
        self.power_state_updated = now()
        mark_ready = (
            self.status == NODE_STATUS.RELEASING and
            power_state == POWER_STATE.OFF)
        if mark_ready:
            # Ensure the node is released when it powers down.
            self.status_expires = None
            self._finalize_release()
        if self.status == NODE_STATUS.EXITING_RESCUE_MODE:
            if self.previous_status == NODE_STATUS.BROKEN:
                if power_state == POWER_STATE.OFF:
                    self.status = self.previous_status
                    self.owner = None
                else:
                    self.status = NODE_STATUS.FAILED_EXITING_RESCUE_MODE
            elif self.previous_status == NODE_STATUS.DEPLOYED:
                if power_state == POWER_STATE.ON:
                    self.status = self.previous_status
                else:
                    self.status = NODE_STATUS.FAILED_EXITING_RESCUE_MODE
        self.save()

    def is_in_allocated_state(self):
        """Return True if the node is in a state where it is allocated.

        See status in `ALLOCATED_NODE_STATUSES`.
        """
        return self.status in ALLOCATED_NODE_STATUSES

    def set_default_storage_layout(self):
        """Sets the default storage layout.

        This is called after a node has been commissioned to set the default
        storage layout. This is done after commissioning because only then
        will the node actually have some `PhysicalBlockDevice`'s.
        """
        # Do nothing if networking should be skipped.
        if self.skip_storage:
            return
        storage_layout = Config.objects.get_config("default_storage_layout")
        try:
            self.set_storage_layout(storage_layout)
        except StorageLayoutMissingBootDiskError:
            maaslog.error(
                "%s: Unable to set any default storage layout because it "
                "has no writable disks.", self.hostname)
        except StorageLayoutError as e:
            maaslog.error(
                "%s: Failed to configure storage layout: %s",
                self.hostname, e)

    def set_storage_layout(self, layout, params={}, allow_fallback=True):
        """Set storage layout for this node."""
        storage_layout = get_storage_layout_for_node(
            layout, self, params=params)
        if storage_layout is not None:
            used_layout = storage_layout.configure(
                allow_fallback=allow_fallback)
            maaslog.info(
                "%s: Storage layout was set to %s.",
                self.hostname, used_layout)
        else:
            raise StorageLayoutError("Unknown storage layout: %s" % layout)

    def _clear_full_storage_configuration(self):
        """Clear's the full storage configuration for this node.

        This will remove all related models to `PhysicalBlockDevice`'s on
        this node and all `VirtualBlockDevice`'s.

        This is used before commissioning to clear the entire storage model
        except for the `PhysicalBlockDevice`'s. Commissioing will update the
        `PhysicalBlockDevice` information on this node.
        """
        physical_block_devices = self.physicalblockdevice_set.all()
        PartitionTable.objects.filter(
            block_device__in=physical_block_devices).delete()
        Filesystem.objects.filter(
            block_device__in=physical_block_devices).delete()
        for block_device in self.virtualblockdevice_set.all():
            try:
                block_device.filesystem_group.delete(force=True)
            except FilesystemGroup.DoesNotExist:
                # When a filesystem group has multiple virtual block devices
                # it is possible that accessing `filesystem_group` will
                # result in it already being deleted.
                pass

    def _create_acquired_filesystems(self):
        """Copy all filesystems that have a user mountable filesystem to be
        in acquired mode.

        Any modification to the filesystems from this point forward should use
        the acquired filesystems instead of the original. The acquired
        filesystems will be removed on release of the node.
        """
        self._clear_acquired_filesystems()
        filesystems = Filesystem.objects.filter_by_node(self).filter(
            fstype__in=FILESYSTEM_FORMAT_TYPE_CHOICES_DICT, acquired=False)
        for filesystem in filesystems:
            filesystem.id = None
            filesystem.acquired = True
            filesystem.save()

    def _clear_acquired_filesystems(self):
        """Clear the filesystems that are created when the node is acquired.
        """
        filesystems = Filesystem.objects.filter_by_node(self).filter(
            acquired=True)
        filesystems.delete()

    def _create_acquired_bridges(self, bridge_stp=None, bridge_fd=None):
        """Create an acquired bridge on all configured interfaces."""
        interfaces = self.interface_set.exclude(type=INTERFACE_TYPE.BRIDGE)
        interfaces = interfaces.prefetch_related('ip_addresses')
        for interface in interfaces:
            if interface.is_configured():
                interface.create_acquired_bridge(
                    bridge_stp=bridge_stp, bridge_fd=bridge_fd)

    def claim_auto_ips(self):
        """Assign IP addresses to all interface links set to AUTO."""
        exclude_addresses = set()
        for interface in self.interface_set.all():
            claimed_ips = interface.claim_auto_ips(
                exclude_addresses=exclude_addresses)
            for ip in claimed_ips:
                exclude_addresses.add(str(ip.ip))

    @transactional
    def release_interface_config(self):
        """Release IP addresses on all interface links set to AUTO and
        remove all acquired bridge interfaces."""
        for interface in self.interface_set.all():
            interface.release_auto_ips()
            if interface.type == INTERFACE_TYPE.BRIDGE and interface.acquired:
                # Move all IP addresses assigned to an acquired bridge to the
                # parent of the bridge.
                parent = interface.parents.first()
                for sip in interface.ip_addresses.all():
                    sip.interface_set.remove(interface)
                    sip.interface_set.add(parent)
                # Delete the acquired bridge interface.
                interface.delete()

    def _clear_networking_configuration(self):
        """Clear the networking configuration for this node.

        The networking configuration is cleared when a node is going to be
        commissioned. This allows the new commissioning data to create a new
        networking configuration.
        """
        self.gateway_link_ipv4 = None
        self.gateway_link_ipv6 = None
        interfaces = self.interface_set.all()
        for interface in interfaces:
            interface.clear_all_links(clearing_config=True)

    def set_initial_networking_configuration(self):
        """Set the networking configuration to the default for this node.

        The networking configuration is set to an initial configuration where
        the boot interface is set to AUTO and all other interfaces are set
        to LINK_UP.

        This is done after commissioning has finished.
        """
        # Do nothing if networking should be skipped.
        if self.skip_networking:
            return

        boot_interface = self.get_boot_interface()
        if boot_interface is None:
            # No interfaces on the node. Nothing to do.
            return

        assert \
            self.status not in [NODE_STATUS.DEPLOYING, NODE_STATUS.DEPLOYED], \
            'Node cannot be in a deploying state when configuring network'

        # Set AUTO mode on the boot interface.
        auto_set = False
        discovered_addresses = boot_interface.ip_addresses.filter(
            alloc_type=IPADDRESS_TYPE.DISCOVERED, subnet__isnull=False)
        for ip_address in discovered_addresses:
            boot_interface.link_subnet(
                INTERFACE_LINK_TYPE.AUTO, ip_address.subnet)
            auto_set = True
        if not auto_set:
            # Failed to set AUTO mode on the boot interface. Lets force an
            # AUTO on a subnet that is on the same VLAN as the
            # interface. If that fails we just set the interface to DHCP with
            # no subnet defined.
            boot_interface.force_auto_or_dhcp_link()

        # Set LINK_UP mode on all the other enabled interfaces.
        for interface in self.interface_set.all():
            if interface == boot_interface:
                # Skip the boot interface as it has already been configured.
                continue
            if interface.enabled:
                interface.ensure_link_up()

    def get_best_guess_for_default_gateways(self):
        """Return the best guess for the default gateways. This is
        either one IPv4 gateway, one IPv6 gateway, or both.

        This is determined by looking at all interfaces on the node and
        selecting the best possible default gateway IP. The criteria below
        is used to select the best possible gateway:

          1. Managed subnets over unmanaged subnets.
          2. Bond and bridge interfaces over physical interfaces.
          3. Node's boot interface over all other interfaces except bonds and
             bridges.
          4. Physical interfaces over VLAN interfaces.
          5. Sticky IP links over user reserved IP links.
          6. User reserved IP links over auto IP links.

        :return: List of (interface ID, subnet ID, gateway IP) tuples.
        :rtype: list
        """
        cursor = connection.cursor()

        # DISTINCT ON returns the first matching row for any given
        # IP family. Using the query's ordering.
        cursor.execute("""
            SELECT DISTINCT ON (family(subnet.gateway_ip))
                interface.id, subnet.id, subnet.gateway_ip
            FROM maasserver_node AS node
            JOIN maasserver_interface AS interface ON
                interface.node_id = node.id
            JOIN maasserver_interface_ip_addresses AS link ON
                link.interface_id = interface.id
            JOIN maasserver_staticipaddress AS staticip ON
                staticip.id = link.staticipaddress_id
            JOIN maasserver_subnet AS subnet ON
                subnet.id = staticip.subnet_id
            JOIN maasserver_vlan AS vlan ON
                vlan.id = subnet.vlan_id
            WHERE
                node.id = %s AND
                subnet.gateway_ip IS NOT NULL AND
                host(subnet.gateway_ip) != '' AND
                staticip.alloc_type != 5 AND /* Ignore DHCP */
                staticip.alloc_type != 6 /* Ignore DISCOVERED */
            ORDER BY
                family(subnet.gateway_ip),
                vlan.dhcp_on DESC,
                CASE
                    WHEN interface.type = 'bond' THEN 1
                    WHEN interface.type = 'bridge' THEN 1
                    WHEN interface.type = 'physical' AND
                        interface.id = node.boot_interface_id THEN 2
                    WHEN interface.type = 'physical' THEN 3
                    WHEN interface.type = 'vlan' THEN 4
                    WHEN interface.type = 'alias' THEN 5
                    ELSE 6
                END,
                CASE
                    WHEN staticip.alloc_type = 1 /* STICKY */
                        THEN 1
                    WHEN staticip.alloc_type = 4 /* USER_RESERVED */
                        THEN 2
                    WHEN staticip.alloc_type = 0 /* AUTO */
                        THEN 3
                    ELSE staticip.alloc_type
                END,
                interface.id
            """, (self.id,))
        return [
            GatewayDefinition._make((found[0], found[1], found[2]))
            for found in cursor.fetchall()
        ]

    def _get_best_interface_from_gateway_link(self, gateway_link):
        """Return the best interface for the `gateway_link` and this node."""
        return gateway_link.interface_set.filter(
            node=self).order_by('type', 'id').first().id

    def _get_gateway_tuple(self, gateway_link):
        """Return a tuple for the interface id, subnet id, and gateway IP for
        the `gateway_link`."""
        return (
            self._get_best_interface_from_gateway_link(
                gateway_link),
            gateway_link.subnet.id,
            gateway_link.subnet.gateway_ip,
            )

    def _get_gateway_tuple_by_family(self, gateways, ip_family):
        """Return the gateway tuple from `gateways` that is in the IP address
        family."""
        for gateway in gateways:
            if IPAddress(gateway[2]).version == ip_family:
                return gateway
        return None

    def get_default_gateways(self):
        """Return the default gateways.

        :return: Return a tuple or tuples with IPv4 and IPv6 gateway
            information.
        :rtype: tuple
        """
        # Get the set gateways on the node.
        gateway_ipv4 = None
        gateway_ipv6 = None
        if self.gateway_link_ipv4 is not None:
            subnet = self.gateway_link_ipv4.subnet
            if subnet is not None:
                if subnet.gateway_ip:
                    gateway_ipv4 = self._get_gateway_tuple(
                        self.gateway_link_ipv4)
        if self.gateway_link_ipv6 is not None:
            subnet = self.gateway_link_ipv6.subnet
            if subnet is not None:
                if subnet.gateway_ip:
                    gateway_ipv6 = self._get_gateway_tuple(
                        self.gateway_link_ipv6)

        # Early out if we already have both gateways.
        if gateway_ipv4 and gateway_ipv6:
            return DefaultGateways._make((gateway_ipv4, gateway_ipv6))

        # Get the best guesses for the missing IP families.
        found_gateways = self.get_best_guess_for_default_gateways()
        if not gateway_ipv4:
            gateway_ipv4 = self._get_gateway_tuple_by_family(
                found_gateways, IPADDRESS_FAMILY.IPv4)
        if not gateway_ipv6:
            gateway_ipv6 = self._get_gateway_tuple_by_family(
                found_gateways, IPADDRESS_FAMILY.IPv6)
        return DefaultGateways._make((gateway_ipv4, gateway_ipv6))

    def get_default_dns_servers(self, ipv4=True, ipv6=True):
        """Return the default DNS servers for this node."""
        # Circular imports.
        from maasserver.dns.zonegenerator import get_dns_server_address

        gateways = self.get_default_gateways()

        # Try first to use DNS servers from default gateway subnets.
        if ipv4 and gateways.ipv4 is not None:
            subnet = Subnet.objects.get(id=gateways.ipv4.subnet_id)
            if subnet.dns_servers is not None and len(subnet.dns_servers) > 0:
                # An IPv4 subnet is hosting the default gateway and has DNS
                # servers defined. IPv4 DNS servers take first-priority.
                return subnet.dns_servers
        if ipv6 and gateways.ipv6 is not None:
            subnet = Subnet.objects.get(id=gateways.ipv6.subnet_id)
            if subnet.dns_servers is not None and len(subnet.dns_servers) > 0:
                # An IPv6 subnet is hosting the default gateway and has DNS
                # servers defined. IPv6 DNS servers take second-priority.
                return subnet.dns_servers

        # No default gateway subnet has specific DNS servers defined, so
        # use MAAS for the default DNS server.
        if gateways.ipv4 is None and gateways.ipv6 is None:
            # If there are no default gateways, the default is the MAAS
            # region IP address.
            maas_dns_server = get_dns_server_address(
                rack_controller=self.get_boot_rack_controller(),
                ipv4=ipv4, ipv6=ipv6)
        else:
            # Choose an address consistent with the primary address-family
            # in use, as indicated by the presence (or not) of a gateway.
            # Note that this path is only taken if the MAAS URL is set to
            # a hostname, and the hostname resolves to both an IPv4 and an
            # IPv6 address.
            maas_dns_server = get_dns_server_address(
                rack_controller=self.get_boot_rack_controller(),
                ipv4=(ipv4 and gateways.ipv4 is not None),
                ipv6=(ipv6 and gateways.ipv6 is not None))
        return [maas_dns_server]

    def get_boot_purpose(self):
        """
        Return a suitable "purpose" for this boot, e.g. "install".
        """
        # XXX: allenap bug=1031406 2012-07-31: The boot purpose is
        # still in flux. It may be that there will just be an
        # "ephemeral" environment and an "install" environment, and
        # the differing behaviour between, say, enlistment and
        # commissioning - both of which will use the "ephemeral"
        # environment - will be governed by varying the preseed or PXE
        # configuration.
        if self.status in COMMISSIONING_LIKE_STATUSES:
            # It is commissioning or disk erasing. The environment (boot
            # images, kernel options, etc for erasing is the same as that
            # of commissioning.
            return "commissioning"
        elif self.status == NODE_STATUS.DEPLOYING:
            # Install the node if netboot is enabled,
            # otherwise boot locally.
            if self.netboot:
                return "xinstall"
            else:
                return "local"
        elif (self.status == NODE_STATUS.DEPLOYED or
                self.node_type != NODE_TYPE.MACHINE):
            return "local"
        else:
            return "poweroff"

    def get_boot_interface(self):
        """Get the boot interface this node is expected to boot from.

        Normally, this will be the boot interface last used in a
        GetBootConfig RPC request for the node, as recorded in the
        'boot_interface' property. However, if the node hasn't booted since
        the 'boot_interface' property was added to the Node model, this will
        return the node's first interface instead.
        """
        if self.boot_interface is not None:
            return self.boot_interface

        # Only use "all" and perform the sorting manually to stop extra queries
        # when the `interface_set` is prefetched.
        interfaces = sorted(self.interface_set.all(), key=attrgetter('id'))
        if len(interfaces) == 0:
            return None
        return interfaces[0]

    def get_boot_rack_controller(self):
        """Return the `RackController` that this node booted from last.

        This uses the `boot_cluster_ip` to determine which rack controller
        this node booted from last. If empty or not a rack controller
        then it will fallback to the primary rack controller for the boot
        interface."""
        rack_controller = None
        if self.boot_cluster_ip is not None:
            rack_controller = RackController.objects.filter(
                interface__ip_addresses__ip=self.boot_cluster_ip).first()
        if rack_controller is None:
            return self.get_boot_primary_rack_controller()
        else:
            return rack_controller

    def get_boot_primary_rack_controller(self):
        """Return the `RackController` that this node will boot from as its
        primary rack controller ."""
        boot_interface = self.get_boot_interface()
        if (boot_interface is None or
                boot_interface.vlan is None or
                not boot_interface.vlan.dhcp_on):
            return None
        else:
            return boot_interface.vlan.primary_rack

    def get_boot_secondary_rack_controller(self):
        """Return the `RackController` that this node will boot from as its
        secondary rack controller ."""
        boot_interface = self.get_boot_interface()
        if (boot_interface is None or
                boot_interface.vlan is None or
                not boot_interface.vlan.dhcp_on):
            return None
        else:
            return boot_interface.vlan.secondary_rack

    def get_boot_rack_controllers(self):
        """Return the `RackController` that this node will boot from."""
        boot_interface = self.get_boot_interface()
        if (boot_interface is None or
                boot_interface.vlan is None or
                not boot_interface.vlan.dhcp_on):
            return []
        else:
            racks = [
                boot_interface.vlan.primary_rack,
                boot_interface.vlan.secondary_rack,
            ]
            return [
                rack
                for rack in racks
                if rack is not None
            ]

    def get_pxe_mac_vendor(self):
        """Return the vendor of the MAC address the node booted from."""
        boot_interface = self.get_boot_interface()
        if boot_interface is None:
            return None
        else:
            return get_vendor_for_mac(boot_interface.mac_address.get_raw())

    def get_extra_macs(self):
        """Get the MACs other that the one the node booted from."""
        boot_interface = self.get_boot_interface()
        # Use all here and not filter on type so the precache is used.
        return [
            interface.mac_address
            for interface in self.interface_set.all()
            if (interface != boot_interface and
                interface.type == INTERFACE_TYPE.PHYSICAL)
        ]

    def status_message(self):
        """Returns a string representation of the most recent event description
        (supplied through the status API) associated with this node, None if
        there are no events."""
        from maasserver.models.event import Event  # Avoid circular import.
        # Id's have a lower (non-zero under heavy load) chance of being out of
        # order than of two timestamps colliding.
        event = Event.objects.filter(node=self).order_by('-id').first()
        return event.description if event is not None else None

    def status_action(self):
        """Returns a string representation of the most recent event action name
        (supplied through the status API) associated with this node, None if
        there are no events."""
        from maasserver.models.event import Event  # Avoid circular import.
        # Id's have a lower (non-zero under heavy load) chance of being out of
        # order than of two timestamps colliding.
        event = Event.objects.filter(node=self).order_by('-id').first()
        return event.action if event is not None else None

    @property
    def status_name(self):
        """Returns the status of the nome as a user-friendly string."""
        return NODE_STATUS_CHOICES_DICT[self.status]

    @transactional
    def start(self, user, user_data=None, comment=None):
        if not user.has_perm(NODE_PERMISSION.EDIT, self):
            # You can't start a node you don't own unless you're an admin.
            raise PermissionDenied()
        event = EVENT_TYPES.REQUEST_NODE_START
        allow_power_cycle = False
        # If status is ALLOCATED, this start is actually for a deployment.
        # (Note: this is true even when nodes are being deployed from READY
        # state. See node_action.py; the node is acquired and then started.)
        # Power cycling is allowed when deployment is being started.
        if self.status == NODE_STATUS.ALLOCATED:
            event = EVENT_TYPES.REQUEST_NODE_START_DEPLOYMENT
            allow_power_cycle = True
        self._register_request_event(
            user, event, action='start', comment=comment)
        return self._start(
            user, user_data, allow_power_cycle=allow_power_cycle)

    def _get_bmc_client_connection_info(self, *args, **kwargs):
        """Return a tuple that list the rack controllers that can communicate
        to the BMC for this node.

        First entry in the tuple is the rack controllers that can communicate
        to a BMC because it has an IP address on a subnet that the rack
        controller also has an IP address.

        Second entry is a fallback to the old way pre-MAAS 2.0 where only
        the rack controller that owned the node could power it on. Here we
        providing the primary and secondary rack controllers that are managing
        the VLAN that this node PXE boots from.
        """
        if self.bmc is None:
            client_idents = []
        else:
            client_idents = self.bmc.get_client_identifiers()
        fallback_idents = [
            rack.system_id
            for rack in self.get_boot_rack_controllers()
        ]
        if len(client_idents) == 0 and len(fallback_idents) == 0:
            err_msg = "No rack controllers can access the BMC of node: %s" % (
                self.hostname)
            self._register_request_event(
                self.owner, EVENT_TYPES.NODE_POWER_QUERY_FAILED,
                "Failed to query node's BMC", err_msg)
            raise PowerProblem(err_msg)
        return client_idents, fallback_idents

    @transactional
    def _start(
            self, user, user_data=None, old_status=None,
            allow_power_cycle=False):
        """Request on given user's behalf that the node be started up.

        :param user: Requesting user.
        :type user: User_
        :param user_data: Optional blob of user-data to be made available to
            the node through the metadata service. If not given, any previous
            user data is used.
        :type user_data: unicode

        :raise StaticIPAddressExhaustion: if there are not enough IP addresses
            left in the static range for this node to get all the addresses it
            needs.
        :raise PermissionDenied: If `user` does not have permission to
            start this node.

        :return: a `Deferred` which contains the post-commit tasks that are
            required to run to start the node. This is already registed as a
            post-commit hook; it should not be added a second time. If it has
            not been possible to start the node because the power controller
            does not support it, `None` will be returned. The node must be
            powered on manually.
        """
        # Avoid circular imports.
        from metadataserver.models import NodeUserData

        if not user.has_perm(NODE_PERMISSION.EDIT, self):
            # You can't start a node you don't own unless you're an admin.
            raise PermissionDenied()

        # Record the user data for the node. Note that we do this
        # whether or not we can actually send power commands to the
        # node; the user may choose to start it manually.
        NodeUserData.objects.set_user_data(self, user_data)

        if self.status == NODE_STATUS.ALLOCATED:
            old_status = self.status
            # Claim AUTO IP addresses for the node if it's ALLOCATED.
            # The current state being ALLOCATED is our indication that the node
            # is being deployed for the first time.
            self.claim_auto_ips()
            deployment_timeout = self.get_deployment_time()
            self._start_deployment()
            claimed_ips = True
        else:
            deployment_timeout = None
            claimed_ips = False

        power_info = self.get_effective_power_info()
        if not power_info.can_be_started:
            # The node can't be powered on by MAAS, so return early.
            # Everything we've done up to this point is still valid;
            # this is not an error state.
            return None

        # Request that the node be powered on post-commit.
        d = post_commit()
        if self.power_state == POWER_STATE.ON and allow_power_cycle:
            d = self._power_control_node(d, power_cycle, power_info)
        else:
            d = self._power_control_node(d, power_on_node, power_info)

        # Set the deployment timeout so the node is marked failed after
        # a period of time.
        if deployment_timeout is not None:
            d.addCallback(
                callOutToDatabase, Node._set_status_expires,
                self.system_id, deployment_timeout)

        if old_status is not None:
            # If there's an error, reset the node's status.
            d.addErrback(
                callOutToDatabase, Node._set_status, self.system_id,
                status=old_status)

        # If any part of this processes fails be sure to release the grabbed
        # auto IP addresses.
        if claimed_ips:
            d.addErrback(callOutToDatabase, self.release_interface_config)
        return d

    @transactional
    def stop(self, user, stop_mode='hard', comment=None):
        if not user.has_perm(NODE_PERMISSION.EDIT, self):
            # You can't stop a node you don't own unless you're an admin.
            raise PermissionDenied()
        self._register_request_event(
            user, EVENT_TYPES.REQUEST_NODE_STOP, action='stop',
            comment=comment)
        return self._stop(user, stop_mode)

    @transactional
    def _stop(self, user, stop_mode='hard'):
        """Request that the node be powered down.

        :param user: Requesting user.
        :type user: User_
        :param stop_mode: Power off mode - usually 'soft' or 'hard'.
        :type stop_mode: unicode

        :raise PermissionDenied: If `user` does not have permission to
            stop this node.

        :return: a `Deferred` which contains the post-commit tasks that are
            required to run to stop the node. This is already registed as a
            post-commit hook; it should not be added a second time. If it has
            not been possible to stop the node because the power controller
            does not support it, `None` will be returned. The node must be
            powered off manually.
        """
        if not user.has_perm(NODE_PERMISSION.EDIT, self):
            # You can't stop a node you don't own unless you're an admin.
            raise PermissionDenied()

        power_info = self.get_effective_power_info()
        if not power_info.can_be_stopped:
            # We can't stop this node, so just return; trying to stop a
            # node we don't know how to stop isn't an error state, but
            # it's a no-op.
            return None

        # Smuggle in a hint about how to power-off the self.
        power_info.power_parameters['power_off_mode'] = stop_mode

        # Request that the node be powered off post-commit.
        d = post_commit()
        return self._power_control_node(d, power_off_node, power_info)

    @asynchronous
    def power_query(self):
        """Query the power state of the BMC for this node.

        This make sure either a layer-2 or a routable connection can be
        determined for the BMC before performing the query.

        This method can be called from within the reactor or will return an
        `EventualResult`. Wait should be called on the result for the desired
        waiting time. Recommend timeout is 45 seconds. 30 seconds for the
        power_query_all and 15 seconds for the power_query.
        """
        # Avoid circular imports.
        from maasserver.models.event import Event
        d = deferToDatabase(transactional(self.get_effective_power_info))

        def cb_query(power_info):
            d = self._power_control_node(
                succeed(None), power_query, power_info)
            d.addCallback(lambda result: (result, power_info))
            return d

        @transactional
        def cb_create_event(result):
            response, _ = result
            power_state = response["state"]
            power_error = (
                response["error_msg"] if "error_msg" in response else None)
            # Add event log for success or failure.
            # Use power_error for failure message.
            if power_error is None:
                message = "Power state queried: %s" % power_state
                Event.objects.create_node_event(
                    system_id=self.system_id,
                    event_type=EVENT_TYPES.NODE_POWER_QUERIED,
                    event_description=message)
            else:
                Event.objects.create_node_event(
                    system_id=self.system_id,
                    event_type=EVENT_TYPES.NODE_POWER_QUERY_FAILED,
                    event_description=power_error)
            return result

        def cb_update_power(result):
            response, power_info = result
            power_state = response["state"]
            if power_info.can_be_queried and self.power_state != power_state:

                @transactional
                def cb_update_queryable_node():
                    self.update_power_state(power_state)
                    return power_state
                return deferToDatabase(cb_update_queryable_node)
            elif not power_info.can_be_queried:

                @transactional
                def cb_update_non_queryable_node():
                    self.update_power_state(POWER_STATE.UNKNOWN)
                    return POWER_STATE.UNKNOWN
                return deferToDatabase(cb_update_non_queryable_node)
            else:
                return power_state

        d.addCallback(cb_query)
        d.addCallback(partial(deferToDatabase, cb_create_event))
        d.addCallback(cb_update_power)
        return d

    def _power_control_node(self, defer, power_method, power_info):
        # Check if the BMC is accessible. If not we need to do some work to
        # make sure we can determine which rack controller can power
        # control this node.
        def is_bmc_accessible():
            if self.bmc is None:
                raise PowerProblem(
                    "No BMC is defined.  Cannot power control node.")
            else:
                return self.bmc.is_accessible()

        defer.addCallback(
            lambda _: deferToDatabase(transactional(is_bmc_accessible)))

        def cb_update_routable_racks(accessible):
            if not accessible:
                # Perform power query on all of the rack controllers to
                # determine which has access to this node's BMC.
                d = power_query_all(
                    self.system_id, self.hostname, power_info)

                @transactional
                def cb_update_routable(result):
                    power_state, routable_racks, non_routable_racks = result
                    if (power_info.can_be_queried and
                            self.power_state != power_state):
                        # MAAS will query power types that even say they don't
                        # support query. But we only update the power_state on
                        # those we are saying MAAS reports on.
                        self.update_power_state(power_state)
                    self.bmc.update_routable_racks(
                        routable_racks, non_routable_racks)

                # Update the routable information for the BMC.
                d.addCallback(partial(
                    deferToDatabase,
                    transactional(cb_update_routable)))
                return d

        # Update routable racks only if the BMC is not accessible.
        defer.addCallback(cb_update_routable_racks)

        # Get the client connection information for the node.
        defer.addCallback(
            partial(deferToDatabase, self._get_bmc_client_connection_info))

        def cb_power_control(result):
            client_idents, fallback_idents = result

            def eb_fallback_clients(failure):
                failure.trap(NoConnectionsAvailable)
                return getClientFromIdentifiers(fallback_idents)

            def cb_check_power_driver(client, power_info):
                d = Node.confirm_power_driver_operable(
                    client, power_info.power_type, client.ident)
                d.addCallback(lambda _: client)
                return d

            # Check that we should just not start with the fallback.
            try_fallback = True
            if len(client_idents) == 0:
                client_idents = fallback_idents
                try_fallback = False

            # Get the client and fallback if needed.
            d = getClientFromIdentifiers(client_idents)
            if try_fallback:
                d.addErrback(eb_fallback_clients)
            d.addCallback(cb_check_power_driver, power_info)
            d.addCallback(
                power_method, self.system_id, self.hostname, power_info)
            return d

        # Power control the node.
        defer.addCallback(cb_power_control)
        return defer

    @classmethod
    @transactional
    def _set_status(cls, system_id, status):
        """Set the status of the node identified by `system_id`.

        This is a convenience for use as a call-back.
        """
        node = cls.objects.get(system_id=system_id)
        node.status = status
        node.save()

    def _power_cycle(self):
        """Power cycle the node."""
        # Request that the node be power cycled post-commit.
        d = post_commit()
        return self._power_control_node(
            d, power_cycle, self.get_effective_power_info())

    @transactional
    def start_rescue_mode(self, user):
        """Start rescue mode."""
        # Avoid circular imports.
        from metadataserver.models import NodeUserData
        from metadataserver.user_data.rescue_mode import generate_user_data

        if not user.has_perm(NODE_PERMISSION.EDIT, self):
            # You can't enter rescue mode on a node you don't own,
            # unless you're an admin.
            raise PermissionDenied()
        # Power type must be configured.
        if self.power_type == '':
            raise UnknownPowerType(
                "Unconfigured power type. "
                "Please configure the power type and try again.")
        # Register event.
        self._register_request_event(
            user, EVENT_TYPES.REQUEST_NODE_START_RESCUE_MODE,
            action='start rescue mode')

        rescue_mode_user_data = generate_user_data(node=self)
        # Record the user data for the node. Note that we do this
        # whether or not we can actually send power commands to the
        # node; the user may choose to start it manually.
        NodeUserData.objects.set_user_data(self, rescue_mode_user_data)

        # We need to mark the node as ENTERING_RESCUE_MODE now to avoid a race
        # when starting multiple nodes. We hang on to old_status just in
        # case the power action fails.
        old_status = self.status
        self.status = NODE_STATUS.ENTERING_RESCUE_MODE
        self.owner = user
        self.save()

        try:
            cycling = self._power_cycle()
        except Exception as error:
            self.status = old_status
            self.save()
            maaslog.error(
                "%s: Could not start rescue mode for node: %s",
                self.hostname, error)
            # Let the exception bubble up, since the UI or API will have to
            # deal with it.
            raise
        else:
            # Don't permit naive mocking of cycling(); it causes too much
            # confusion when testing. Return a Deferred from side_effect.
            assert isinstance(cycling, Deferred) or cycling is None

            post_commit().addCallback(
                callOutToDatabase, Node._set_status_expires,
                self.system_id, self.get_entering_rescue_mode_time())

            if cycling is None:
                cycling = post_commit()
                # MAAS cannot start the node itself.
                is_cycling = False
            else:
                # MAAS can direct the node to start.
                is_cycling = True

            cycling.addCallback(
                callOut, self._start_rescue_mode_async, is_cycling,
                self.hostname)

            # If there's an error, reset the node's status.
            cycling.addErrback(
                callOutToDatabase, Node._set_status, self.system_id,
                status=old_status)

            def eb_start(failure, hostname):
                maaslog.error(
                    "%s: Could not start rescue mode for node: %s",
                    hostname, failure.getErrorMessage())
                return failure  # Propagate.

            return cycling.addErrback(eb_start, self.hostname)

    @classmethod
    @asynchronous
    def _start_rescue_mode_async(cls, is_cycling, hostname):
        """Start rescue mode, the post-commit bits.

        :param is_cycling: A boolean indicating if MAAS is able to start this
            node itself, or if manual intervention is needed.
        :param hostname: The node's hostname, for logging.
        """
        if is_cycling:
            maaslog.info("%s: Rescue mode starting", hostname)
        else:
            maaslog.warning(
                "%s: Could not start rescue mode for node; it "
                "must be started manually", hostname)

    @transactional
    def stop_rescue_mode(self, user):
        """Exit rescue mode."""
        if not user.has_perm(NODE_PERMISSION.EDIT, self):
            # You can't exit rescue mode on a node you don't own,
            # unless you're an admin.
            raise PermissionDenied()
        # Register event.
        self._register_request_event(
            user, EVENT_TYPES.REQUEST_NODE_STOP_RESCUE_MODE,
            action='stop rescue mode')
        # We need to mark the node as EXITING_RESCUE_MODE now to avoid a race
        # when starting multiple nodes. We hang on to old_status just in
        # case the power action fails.
        old_status = self.status
        self.status = NODE_STATUS.EXITING_RESCUE_MODE
        self.save()

        try:
            if self.previous_status == NODE_STATUS.BROKEN:
                self._stop(user)
            elif self.previous_status == NODE_STATUS.DEPLOYED:
                self._power_cycle()
        except Exception as error:
            self.status = old_status
            self.save()
            maaslog.error(
                "%s: Could not stop rescue mode for node: %s",
                self.hostname, error)
            # Let the exception bubble up, since the UI or API will have to
            # deal with it.
            raise

    def _as(self, model):
        """Create a `model` that shares underlying storage with `self`.

        In other words, the newly returned object will be an instance of
        `model` and its `__dict__` will be `self.__dict__`. Not a copy, but a
        reference to, so that changes to one will be reflected in the other.
        """
        new = object.__new__(model)
        new.__dict__ = self.__dict__
        return new

    def as_node(self):
        """Return a reference to self that behaves as a `Node`."""
        return self._as(Node)

    def as_machine(self):
        """Return a reference to self that behaves as a `Machine`."""
        return self._as(Machine)

    def as_device(self):
        """Return a reference to self that behaves as a `Device`."""
        return self._as(Device)

    def as_region_controller(self):
        """Return a reference to self that behaves as a `RegionController`."""
        return self._as(RegionController)

    def as_rack_controller(self):
        """Return a reference to self that behaves as a `RackController`."""
        return self._as(RackController)

    _as_self = {
        NODE_TYPE.DEVICE: as_device,
        NODE_TYPE.MACHINE: as_machine,
        NODE_TYPE.RACK_CONTROLLER: as_rack_controller,
        # XXX ltrager 18-02-2016 - Currently only rack controllers have
        # unique functionality so when combined return a rack controller
        NODE_TYPE.REGION_AND_RACK_CONTROLLER: as_rack_controller,
        NODE_TYPE.REGION_CONTROLLER: as_region_controller,
    }

    def as_self(self):
        """Return a reference to self that behaves as its own type."""
        return self._as_self[self.node_type](self)


# Piston serializes objects based on the object class.
# Here we define a proxy class so that we can specialize how devices are
# serialized on the API.    def get_primary_rack_controller(self):
class Machine(Node):
    """An installable node."""

    objects = MachineManager()

    class Meta(DefaultMeta):
        proxy = True

    def __init__(self, *args, **kwargs):
        super(Machine, self).__init__(
            node_type=NODE_TYPE.MACHINE, *args, **kwargs)


class Controller(Node):
    """A node which is either a rack or region controller."""

    objects = ControllerManager()

    class Meta(DefaultMeta):
        proxy = True

    def __init__(self, *args, **kwargs):
        super(Controller, self).__init__(*args, **kwargs)

    def _was_probably_machine(self):
        """Best guess if a rack was a machine.

        MAAS doesn't track node transitions so we have to look at
        breadcrumbs. The first is the status. Only machines can have their
        status changed to something other than NEW and a rack controller should
        only be installable when the machine is in a deployed state.  Second a
        machine must have power information."""
        return self.status == NODE_STATUS.DEPLOYED and self.bmc is not None

    def _update_interface(self, name, config):
        """Update a interface.

        :param name: Name of the interface.
        :param config: Interface dictionary that was parsed from
            /etc/network/interfaces on the rack controller.
        """
        if config["type"] == "physical":
            return self._update_physical_interface(name, config)
        elif config["type"] == "vlan":
            return self._update_vlan_interface(name, config)
        elif config["type"] == "bond":
            return self._update_bond_interface(name, config)
        elif config["type"] == "bridge":
            return self._update_bridge_interface(name, config)
        else:
            raise ValueError(
                "Unkwown interface type '%s' for '%s'." % (
                    config["type"], name))

    def _update_physical_interface(self, name, config):
        """Update a physical interface.

        :param name: Name of the interface.
        :param config: Interface dictionary that was parsed from
            /etc/network/interfaces on the rack controller.
        """
        new_vlan = None
        mac_address = config["mac_address"]
        interface, created = PhysicalInterface.objects.get_or_create(
            mac_address=mac_address, defaults={
                "node": self,
                "name": name,
                "enabled": config["enabled"],
            })
        if created:
            # Make sure that the VLAN on the interface is correct. When
            # links exists on this interface we place it into the correct
            # VLAN. If it cannot be determined and its a new interface it
            # gets placed on its own fabric.
            connected_to_subnets = self._get_connected_subnets(
                config["links"])
            if len(connected_to_subnets) == 0:
                # Not connected to any known subnets. We add it to its own
                # fabric unless this is the very first interface on this
                # rack controller. The first interface always goes on
                # the default VLAN on the default fabric.
                if Interface.objects.filter(node=self).count() > 1:
                    new_fabric = Fabric.objects.create()
                    new_vlan = new_fabric.get_default_vlan()
                    interface.vlan = new_vlan
                    interface.save()
                else:
                    interface.vlan = (
                        Fabric.objects.get_default_fabric().get_default_vlan())
                    interface.save()
        else:
            if interface.node.id != self.id:
                # MAC address was on a different node. We need to move
                # it to its new owner. In the process we delete all of its
                # current links because they are completely wrong.
                interface.ip_addresses.all().delete()
                interface.node = self
            interface.name = name
            interface.enabled = config["enabled"]
            interface.save()

        # Update all the IP address on this interface. Fix the VLAN the
        # interface belongs to so its the same as the links.
        update_ip_addresses = self._update_links(interface, config["links"])
        linked_vlan = self._get_first_sticky_vlan_from_ip_addresses(
            update_ip_addresses)
        if linked_vlan is not None:
            interface.vlan = linked_vlan
            interface.save()
            if new_vlan is not None and linked_vlan.id != new_vlan.id:
                # Create a new VLAN for this interface and it was not used as
                # a link re-assigned the VLAN this interface is connected to.
                new_vlan.fabric.delete()
        return interface

    def _get_or_create_vlan_interface(self, name, vlan, parent):
        """Wrapper to get_or_create for VLAN interfaces.

        Ensures all required parameters for updating a VLAN interface on a
        controller are always passed in. (Numerous bugs have occurred because
        one of the parameters have been missed in the _update_vlan_interface()
        below.)

        Also updates the interface name if it is different from what was
        specified.
        """
        interface, created = VLANInterface.objects.get_or_create(
            node=self, vlan=vlan, parents=[parent], defaults={"name": name})
        if interface.name != name:
            interface.name = name
            interface.save()
        return interface, created

    def _update_vlan_interface(self, name, config):
        """Update a VLAN interface.

        :param name: Name of the interface.
        :param config: Interface dictionary that was parsed from
            /etc/network/interfaces on the rack controller.
        """
        # VLAN only ever has one parent, and the parent should always
        # exists because of the order the links are processed.
        parent_name = config["parents"][0]
        parent_nic = Interface.objects.get(node=self, name=parent_name)
        parent_has_links = parent_nic.ip_addresses.filter(
            alloc_type=IPADDRESS_TYPE.STICKY).count() > 0
        update_links = True
        if parent_has_links:
            # If the parent interface has links then we assume that is
            # connected to the correct fabric. This VLAN interface must
            # exist on that fabric.
            vlan, _ = VLAN.objects.get_or_create(
                fabric=parent_nic.vlan.fabric, vid=config["vid"])
            interface, _ = self._get_or_create_vlan_interface(
                name=name, vlan=vlan, parent=parent_nic)
        else:
            # Parent has no links, so we cannot assume that parent is on the
            # correct fabric.
            connected_to_subnets = self._get_connected_subnets(
                config["links"])
            if len(connected_to_subnets) > 0:
                # This VLAN interface has links so lets see if the connected
                # subnet exists on the VID.
                subnet = list(connected_to_subnets)[0]
                if subnet.vlan.vid != config["vid"]:
                    # The matching subnet is not on a VLAN that has the
                    # same VID as the configured interface. The best option
                    # we can do is to log the error and create a new VLAN
                    # on the parents fabric without adding links to this
                    # interface.
                    update_links = False
                    vlan, _ = VLAN.objects.get_or_create(
                        fabric=parent_nic.vlan.fabric, vid=config["vid"])
                    interface, created = self._get_or_create_vlan_interface(
                        name=name, vlan=vlan, parent=parent_nic)
                    if not created:
                        # Interface already existed so remove all assigned IP
                        # addresses.
                        for ip_address in interface.ip_addresses.exclude(
                                alloc_type=IPADDRESS_TYPE.DISCOVERED):
                            interface.unlink_ip_address(ip_address)
                    maaslog.error(
                        "Unable to correctly identify VLAN for interface '%s' "
                        "on controller '%s'. Placing interface on VLAN "
                        "'%s.%d' without address assignments." % (
                            name, self.hostname, vlan.fabric.name, vlan.vid))
                else:
                    # Subnet is on matching VLAN. Create the VLAN interface
                    # on this VLAN and update the parent interface fabric if
                    # needed.
                    interface, _ = self._get_or_create_vlan_interface(
                        name=name, vlan=subnet.vlan, parent=parent_nic)
                    if parent_nic.vlan.fabric_id != subnet.vlan.fabric_id:
                        parent_nic.vlan = subnet.vlan.fabric.get_default_vlan()
                        parent_nic.save()
            else:
                # This VLAN interface has no links and neither does the parent
                # interface. Assume that the parent fabric is correct and
                # place the interface on the VLAN for that fabric.
                vlan, _ = VLAN.objects.get_or_create(
                    fabric=parent_nic.vlan.fabric, vid=config["vid"])
                interface, _ = self._get_or_create_vlan_interface(
                    name=name, vlan=vlan, parent=parent_nic)

        # Update all assigned IP address to the interface. This is not
        # performed when the subnet and VID for that subnet do not match.
        if update_links:
            self._update_links(
                interface, config["links"], force_vlan=True)
        return interface

    def _update_child_interface(self, name, config, child_type):
        """Update a child interface.

        :param name: Name of the interface.
        :param config: Interface dictionary that was parsed from
            /etc/network/interfaces on the rack controller.
        """
        # Get all the parent interfaces for this interface. All the parents
        # should exists because of the order the links are processed.
        ifnames = config["parents"]
        parent_nics = Interface.objects.get_interfaces_on_node_by_name(
            self, ifnames)

        # Ignore most child interfaces that don't have parents. MAAS won't know
        # what to do with them since they can't be connected to a fabric.
        # Bridges are an exception since some MAAS demo/test environments
        # contain virtual bridges.
        if len(parent_nics) == 0 and child_type is not BridgeInterface:
            return None

        mac_address = config["mac_address"]
        interface = child_type.objects.get_or_create_on_node(
            self, name, mac_address, parent_nics)

        links = config["links"]
        found_vlan = self._configure_vlan_from_links(
            interface, parent_nics, links)

        # Update all the IP address on this interface. Fix the VLAN the
        # interface belongs to so its the same as the links and all parents to
        # be on the same VLAN.
        update_ip_addresses = self._update_links(
            interface, links, use_interface_vlan=found_vlan)
        self._update_parent_vlans(interface, parent_nics, update_ip_addresses)
        return interface

    def _update_bond_interface(self, name, config):
        """Update a bond interface.

        :param name: Name of the interface.
        :param config: Interface dictionary that was parsed from
            /etc/network/interfaces on the rack controller.
        """
        return self._update_child_interface(name, config, BondInterface)

    def _update_bridge_interface(self, name, config):
        """Update a bridge interface.

        :param name: Name of the interface.
        :param config: Interface dictionary that was parsed from
            /etc/network/interfaces on the rack controller.
        """
        return self._update_child_interface(name, config, BridgeInterface)

    def _update_parent_vlans(
            self, interface, parent_nics, update_ip_addresses):
        """Given the specified interface model object, the specified list of
        parent interfaces, and the specified list of static IP addresses,
        update the parent interfaces to correspond to the VLAN found on the
        subnet the IP address is allocated from.

        If a static IP address is allocated, give preferential treatment to
        the VLAN that IP address resides on.
        """
        linked_vlan = self._get_first_sticky_vlan_from_ip_addresses(
            update_ip_addresses)
        if linked_vlan is not None:
            interface.vlan = linked_vlan
            interface.save()
            for parent_nic in parent_nics:
                if parent_nic.vlan_id != linked_vlan.id:
                    parent_nic.vlan = linked_vlan
                    parent_nic.save()

    def _configure_vlan_from_links(self, interface, parent_nics, links):
        """Attempt to configure the interface VLAN based on the links and
        connected subnets. Returns True if the VLAN was configured; otherwise,
        returns False."""
        # Make sure that the VLAN on the interface is correct. When
        # links exists on this interface we place it into the correct
        # VLAN. If it cannot be determined it is placed on the same fabric
        # as its first parent interface.
        connected_to_subnets = self._get_connected_subnets(links)
        if len(connected_to_subnets) == 0 and len(parent_nics) > 0:
            # Not connected to any known subnets. We add it to the same
            # VLAN as its first parent.
            interface.vlan = parent_nics[0].vlan
            interface.save()
            return True
        elif len(connected_to_subnets) > 0:
            subnet = next(iter(connected_to_subnets))
            interface.vlan = subnet.vlan
            interface.save()
            return True
        return False

    def _get_connected_subnets(self, links):
        """Return a set of subnets that `links` belongs to."""
        subnets = set()
        for link in links:
            if link["mode"] == "static":
                ip_addr = IPNetwork(link["address"])
                subnet = get_one(Subnet.objects.filter(cidr=str(ip_addr.cidr)))
                if subnet is not None:
                    subnets.add(subnet)
        return subnets

    def _get_alloc_type_from_ip_addresses(self, alloc_type, ip_addresses):
        """Return IP address from `ip_addresses` that is first
        with `alloc_type`."""
        for ip_address in ip_addresses:
            if alloc_type == ip_address.alloc_type:
                return ip_address
        return None

    def _get_ip_address_from_ip_addresses(self, ip, ip_addresses):
        """Return IP address from `ip_addresses` that matches `ip`."""
        for ip_address in ip_addresses:
            if ip == ip_address.ip:
                return ip_address
        return None

    def _get_first_sticky_vlan_from_ip_addresses(self, ip_addresses):
        """Return the first VLAN for a STICKY IP address in `ip_addresses`."""
        for ip_address in ip_addresses:
            if ip_address.alloc_type == IPADDRESS_TYPE.STICKY:
                return ip_address.subnet.vlan
        return None

    def _update_links(
            self, interface, links, force_vlan=False, use_interface_vlan=True):
        """Update the links on `interface`."""
        interface.ip_addresses.filter(
            alloc_type=IPADDRESS_TYPE.DISCOVERED).delete()
        current_ip_addresses = list(
            interface.ip_addresses.exclude(
                alloc_type=IPADDRESS_TYPE.DISCOVERED))
        updated_ip_addresses = set()
        if use_interface_vlan:
            vlan = interface.vlan
        elif len(links) > 0:
            fabric = Fabric.objects.create()
            vlan = fabric.get_default_vlan()
            interface.vlan = vlan
            interface.save()
        for link in links:
            if link["mode"] == "dhcp":
                dhcp_address = self._get_alloc_type_from_ip_addresses(
                    IPADDRESS_TYPE.DHCP, current_ip_addresses)
                if dhcp_address is None:
                    dhcp_address = StaticIPAddress.objects.create(
                        alloc_type=IPADDRESS_TYPE.DHCP, ip=None, subnet=None)
                    dhcp_address.save()
                    interface.ip_addresses.add(dhcp_address)
                else:
                    current_ip_addresses.remove(dhcp_address)
                if "address" in link:
                    # DHCP IP address was discovered. Add it as a discovered
                    # IP address.
                    ip_network = IPNetwork(link["address"])
                    ip_addr = str(ip_network.ip)

                    # Get or create the subnet for this link. If created if
                    # will be added to the VLAN on the interface.
                    subnet, _ = Subnet.objects.get_or_create(
                        cidr=str(ip_network.cidr), defaults={
                            "name": str(ip_network.cidr),
                            "vlan": vlan,
                        })

                    # Make sure that the subnet is on the same VLAN as the
                    # interface.
                    if force_vlan and subnet.vlan_id != interface.vlan_id:
                        maaslog.error(
                            "Unable to update IP address '%s' assigned to "
                            "interface '%s' on controller '%s'. "
                            "Subnet '%s' for IP address is not on "
                            "VLAN '%s.%d'." % (
                                ip_addr, interface.name, self.hostname,
                                subnet.name, subnet.vlan.fabric.name,
                                subnet.vlan.vid))
                        continue

                    # Create the DISCOVERED IP address.
                    ip_address, created = (
                        StaticIPAddress.objects.get_or_create(
                            ip=ip_addr, defaults={
                                "alloc_type": IPADDRESS_TYPE.DISCOVERED,
                                "subnet": subnet,
                            }))
                    if not created:
                        ip_address.alloc_type = IPADDRESS_TYPE.DISCOVERED
                        ip_address.subnet = subnet
                        ip_address.save()
                    interface.ip_addresses.add(ip_address)
                updated_ip_addresses.add(dhcp_address)
            elif link["mode"] == "static":
                ip_network = IPNetwork(link["address"])
                ip_addr = str(ip_network.ip)

                # Get or create the subnet for this link. If created if will
                # be added to the VLAN on the interface.
                subnet, _ = Subnet.objects.get_or_create(
                    cidr=str(ip_network.cidr), defaults={
                        "name": str(ip_network.cidr),
                        "vlan": vlan,
                    })

                # Make sure that the subnet is on the same VLAN as the
                # interface.
                if force_vlan and subnet.vlan_id != interface.vlan_id:
                    maaslog.error(
                        "Unable to update IP address '%s' assigned to "
                        "interface '%s' on controller '%s'. Subnet '%s' "
                        "for IP address is not on VLAN '%s.%d'." % (
                            ip_addr, interface.name, self.hostname,
                            subnet.name, subnet.vlan.fabric.name,
                            subnet.vlan.vid))
                    continue

                # Update the gateway on the subnet if one is not set.
                if (subnet.gateway_ip is None and
                        "gateway" in link and
                        IPAddress(link["gateway"]) in subnet.get_ipnetwork()):
                    subnet.gateway_ip = link["gateway"]
                    subnet.save()

                # Determine if this interface already has this IP address.
                ip_address = self._get_ip_address_from_ip_addresses(
                    ip_addr, current_ip_addresses)
                if ip_address is None:
                    # IP address is not assigned to this interface. Get or
                    # create that IP address.
                    ip_address, created = (
                        StaticIPAddress.objects.get_or_create(
                            ip=ip_addr, defaults={
                                "alloc_type": IPADDRESS_TYPE.STICKY,
                                "subnet": subnet,
                            }))
                    if not created:
                        ip_address.alloc_type = IPADDRESS_TYPE.STICKY
                        ip_address.subnet = subnet
                        ip_address.save()
                else:
                    current_ip_addresses.remove(ip_address)

                # Update the properties and make sure all interfaces
                # assigned to the address belong to this node.
                for attached_nic in ip_address.interface_set.all():
                    if attached_nic.node.id != self.id:
                        attached_nic.ip_addresses.remove(ip_address)
                ip_address.alloc_type = IPADDRESS_TYPE.STICKY
                ip_address.subnet = subnet
                ip_address.save()

                # Add this IP address to the interface.
                interface.ip_addresses.add(ip_address)
                updated_ip_addresses.add(ip_address)

        # Remove all the current IP address that no longer apply to this
        # interface.
        for ip_address in current_ip_addresses:
            interface.unlink_ip_address(ip_address)

        return updated_ip_addresses

    def report_neighbours(self, neighbours):
        """Update the neighbour table for this controller.

        :param neighbours: A list of dictionaries containing neighbour data.
            Neighbour data is gathered directly from the ARP monitoring process
            running on each rack interface.
        """
        # Determine which interfaces' neighbours need updating.
        interface_set = {neighbour['interface'] for neighbour in neighbours}
        interfaces = Interface.objects.get_interface_dict_for_node(
            self, names=interface_set, fetch_fabric_vlan=True)
        for neighbour in neighbours:
            interface = interfaces.get(neighbour['interface'], None)
            if interface is not None:
                interface.update_neighbour(neighbour)
                vid = neighbour.get("vid", None)
                if vid is not None:
                    interface.report_vid(vid)

    def report_mdns_entries(self, entries):
        """Update the mDNS entries on this controller.

        :param entries: A list of dictionaries containing discovered mDNS
            entries. mDNS data is gathered from an `avahi-browse` process
            running on each rack interface.
        """
        # Determine which interfaces' entries need updating.
        interface_set = {entry['interface'] for entry in entries}
        interfaces = Interface.objects.get_interface_dict_for_node(
            self, names=interface_set)
        for entry in entries:
            interface = interfaces.get(entry['interface'], None)
            if interface is not None:
                interface.update_mdns_entry(entry)

    def get_discovery_state(self):
        """Returns the interface monitoring state for this Controller.

        The returned object must be suitable to serialize into JSON for RPC
        purposes.
        """
        interfaces = self.interface_set.all()
        return {
            interface.name: interface.get_discovery_state()
            for interface in interfaces
        }

    def update_interfaces(self, interfaces):
        """Update the interfaces attached to the controller.

        :param interfaces: Interfaces dictionary that was parsed from
            /etc/network/interfaces on the controller.
        """
        # Get all of the current interfaces on this controller.
        current_interfaces = {
            interface.id: interface
            for interface in self.interface_set.all().order_by('id')
        }

        # Update the interfaces in dependency order. This make sure that the
        # parent is created or updated before the child. The order inside
        # of the sorttop result is ordered so that the modification locks that
        # postgres grabs when updating interfaces is always in the same order.
        # The ensures that multiple threads can call this method at the
        # exact same time. Without this ordering it will deadlock because
        # multiple are trying to update the same items in the database in
        # a different order.
        process_order = sorttop({
            name: config["parents"]
            for name, config in interfaces.items()
        })
        process_order = [
            sorted(list(items))
            for items in process_order
        ]
        # Cache the neighbour discovery settings, since they will be used for
        # every interface on this Controller.
        discovery_mode = Config.objects.get_network_discovery_config()
        for name in flatten(process_order):
            settings = interfaces[name]
            # Note: the interface that comes back from this call may be None,
            # if we decided not to model an interface based on what the rack
            # sent.
            interface = self._update_interface(name, settings)
            if interface is not None:
                interface.update_discovery_state(discovery_mode, settings)
            if interface is not None and interface.id in current_interfaces:
                del current_interfaces[interface.id]

        # Remove all the interfaces that no longer exist. We do this in reverse
        # order so the child is deleted before the parent.
        deletion_order = {}
        for nic_id, nic in current_interfaces.items():
            deletion_order[nic_id] = [
                parent.id
                for parent in nic.parents.all()
                if parent.id in current_interfaces
            ]
        deletion_order = sorttop(deletion_order)
        deletion_order = [
            sorted(list(items))
            for items in deletion_order
        ]
        deletion_order = reversed(list(flatten(deletion_order)))
        for delete_id in deletion_order:
            if self.boot_interface_id == delete_id:
                self.boot_interface = None
            current_interfaces[delete_id].delete()
        self.save()

    @transactional
    def _get_token_for_controller(self):
        # Avoid circular imports.
        from metadataserver.models import NodeKey
        token = NodeKey.objects.get_token_for_node(self)
        # Pull consumer into memory so it can be accessed outside a
        # database thread
        token.consumer
        return token

    @transactional
    def _signal_start_of_refresh(self):
        self._register_request_event(
            self.owner, EVENT_TYPES.REQUEST_CONTROLLER_REFRESH,
            action='starting refresh')

    @transactional
    def _process_sys_info(self, response):
        update_fields = []
        if response['hostname'] != '':
            self.hostname = response['hostname']
            update_fields.append('hostname')
        if response['architecture'] != '':
            self.architecture = response['architecture']
            update_fields.append('architecture')
        if response['osystem'] != '':
            self.osystem = response['osystem']
            update_fields.append('osystem')
        if response['distro_series'] != '':
            self.distro_series = response['distro_series']
            update_fields.append('distro_series')
        if response['interfaces'] != {}:
            self.update_interfaces(response['interfaces'])
        if len(update_fields) > 0:
            self.save(update_fields=update_fields)

    def update_discovery_state(self, discovery_mode):
        """Update network discovery state on this Controller's interfaces.

        The `discovery_mode` parameter must be a NetworkDiscoveryConfig tuple.

        Returns the `interfaces` dictionary used during processing.
        """
        # Get the interfaces in the [rough] format of the region/rack contract.
        interfaces = Interface.objects.get_all_interfaces_definition_for_node(
            self)
        # Use the data to calculate which interfaces should be monitored by
        # default on this controller, then update each interface.
        annotate_with_default_monitored_interfaces(interfaces)
        for settings in interfaces.values():
            interface = settings['obj']
            interface.update_discovery_state(discovery_mode, settings)
        return interfaces


class RackController(Controller):
    """A node which is running rackd."""

    objects = RackControllerManager()

    class Meta(DefaultMeta):
        proxy = True

    def __init__(self, *args, **kwargs):
        super(RackController, self).__init__(
            node_type=NODE_TYPE.RACK_CONTROLLER, *args, **kwargs)

    @inlineCallbacks
    def refresh(self):
        """Refresh the hardware and networking columns of the rack controller.

        :raises NoConnectionsAvailable: If no connections to the cluster
            are available.
        """
        if self.system_id == get_maas_id():
            # If the refresh is occuring on the running region execute it using
            # the region process. This avoids using RPC and sends the node
            # results back to this host when in HA.
            yield self.as_region_controller().refresh()
            return

        client = yield getClientFor(self.system_id, timeout=1)

        token = yield deferToDatabase(self._get_token_for_controller)

        yield deferToDatabase(self._signal_start_of_refresh)

        try:
            response = yield deferWithTimeout(
                30, client, RefreshRackControllerInfo,
                system_id=self.system_id, consumer_key=token.consumer.key,
                token_key=token.key, token_secret=token.secret)
        except RefreshAlreadyInProgress:
            # If another refresh is in progress let the other process
            # handle it and don't update the database.
            return
        else:
            yield deferToDatabase(self._process_sys_info, response)

    def add_chassis(
            self, user, chassis_type, hostname, username=None, password=None,
            accept_all=False, domain=None, prefix_filter=None,
            power_control=None, port=None, protocol=None):
        self._register_request_event(
            self.owner,
            EVENT_TYPES.REQUEST_RACK_CONTROLLER_ADD_CHASSIS,
            action="Adding chassis %s" % hostname)
        client = getClientFor(self.system_id, timeout=1)
        call = client(
            AddChassis, user=user, chassis_type=chassis_type,
            hostname=hostname, username=username, password=password,
            accept_all=accept_all, domain=domain,
            prefix_filter=prefix_filter, power_control=power_control,
            port=port, protocol=protocol)
        call.wait(30)

    def get_bmc_accessible_nodes(self):
        """Return `QuerySet` of nodes that this rack controller can access.

        This looks at the IP address assigned to all BMC's and filters out
        only the BMC's this rack controller can access. Returning all nodes
        connected to those BMCs.
        """
        subnet_ids = set()
        for interface in self.interface_set.all().prefetch_related(
                "ip_addresses"):
            for ip_address in interface.ip_addresses.all():
                if ip_address.ip and ip_address.subnet_id is not None:
                    subnet_ids.add(ip_address.subnet_id)

        nodes = Node.objects.filter(
            bmc__ip_address__ip__isnull=False,
            bmc__ip_address__subnet_id__in=subnet_ids).distinct()
        return nodes

    def delete(self):
        """Delete this rack controller."""
        # Avoid circular imports
        from maasserver.models import RegionRackRPCConnection

        primary_vlans = VLAN.objects.filter(primary_rack=self)
        if len(primary_vlans) != 0:
            raise ValidationError(
                "Unable to delete '%s'; it is currently set as a primary rack"
                " controller on VLANs %s" %
                (self.hostname,
                    ', '.join([str(vlan) for vlan in primary_vlans])))

        # Disable and delete all services related to this node
        Service.objects.mark_dead(self, dead_rack=True)
        Service.objects.filter(node=self).delete()

        try:
            client = getClientFor(self.system_id, timeout=1)
            call = client(DisableAndShutoffRackd)
            call.wait(30)
        except NoConnectionsAvailable:
            # NoConnectionsAvailable is always thrown. Either because the rack
            # is currently disconnected or rackd was killed
            pass

        RegionRackRPCConnection.objects.filter(rack_controller=self).delete()

        for vlan in VLAN.objects.filter(secondary_rack=self):
            vlan.secondary_rack = None
            vlan.save()

        if self.node_type == NODE_TYPE.REGION_AND_RACK_CONTROLLER:
            self.node_type = NODE_TYPE.REGION_CONTROLLER
            self.save()
        elif self._was_probably_machine():
            self.node_type = NODE_TYPE.MACHINE
            self.save()
        else:
            super().delete()

    def update_rackd_status(self):
        """Update the status of the "rackd" service for this rack controller.

        The "rackd" service status is determined based on the number of
        connections it has to all region controller processes.
        """
        # Circular imports.
        from maasserver.models import (
            RegionRackRPCConnection,
            RegionControllerProcess,
        )
        connections = RegionRackRPCConnection.objects.filter(
            rack_controller=self).prefetch_related("endpoint__process")
        if len(connections) == 0:
            # Not connected to any regions so the rackd is considered dead.
            Service.objects.mark_dead(self, dead_rack=True)
        else:
            connected_to_processes = set(
                conn.endpoint.process
                for conn in connections
            )
            all_processes = set(RegionControllerProcess.objects.all())
            dead_regions = RegionController.objects.exclude(
                processes__in=all_processes).count()
            missing_processes = all_processes - connected_to_processes
            if dead_regions == 0 and len(missing_processes) == 0:
                # Connected to all processes.
                Service.objects.update_service_for(
                    self, "rackd", SERVICE_STATUS.RUNNING)
            else:
                # Not connected to all regions.
                missing_regions = set(
                    process.region
                    for process in missing_processes
                )
                Service.objects.update_service_for(
                    self, "rackd", SERVICE_STATUS.DEGRADED,
                    "Missing connections to %d region controller(s)." % (
                        dead_regions + len(missing_regions)))

    def get_image_sync_status(self, boot_images=None):
        """Return the status of the boot image import process."""
        # Avoid circular imports.
        from maasserver import bootresources
        from maasserver.clusterrpc.boot_images import get_boot_images
        try:
            if bootresources.is_import_resources_running():
                status = "region-importing"
            else:
                if boot_images is None:
                    boot_images = get_boot_images(self)
                if BootResource.objects.boot_images_are_in_sync(boot_images):
                    status = "synced"
                else:
                    if self.is_import_boot_images_running():
                        status = "syncing"
                    else:
                        status = "out-of-sync"
        except (NoConnectionsAvailable, TimeoutError):
            status = 'unknown'
        return status

    def list_boot_images(self):
        """Return a list of boot images available on the rack controller."""
        # Avoid circular imports.
        from maasserver.clusterrpc.boot_images import get_boot_images
        try:
            # Combine all boot images one per name and arch
            downloaded_boot_images = defaultdict(set)
            boot_images = get_boot_images(self)
            for image in boot_images:
                if image['osystem'] == 'custom':
                    image_name = image['release']
                else:
                    image_name = "%s/%s" % (image['osystem'], image['release'])
                image_arch = image['architecture']
                image_subarch = image['subarchitecture']
                downloaded_boot_images[image_name, image_arch].add(
                    image_subarch)

            # Return a list of dictionaries each containing one entry per
            # name, architecture like boot-resources does
            images = [{
                'name': name,
                'architecture': arch,
                'subarches': sorted(subarches),
            } for (name, arch), subarches in downloaded_boot_images.items()]
            status = self.get_image_sync_status(boot_images)
            return {'images': images, 'connected': True, 'status': status}
        except (NoConnectionsAvailable, TimeoutError):
            return {'images': [], 'connected': False, 'status': 'unknown'}

    def is_import_boot_images_running(self):
        """Return whether the boot images are running

        :raises NoConnectionsAvailable: When no connections to the rack
            controller are available for use.
        :raises crochet.TimeoutError: If a response has not been received
            within 30 seconds.
        """
        client = getClientFor(self.system_id, timeout=1)
        call = client(IsImportBootImagesRunning)
        response = call.wait(30)
        return response['running']


class RegionController(Controller):
    """A node which is running multiple regiond's."""

    objects = RegionControllerManager()

    class Meta(DefaultMeta):
        proxy = True

    def __init__(self, *args, **kwargs):
        super(RegionController, self).__init__(
            node_type=NODE_TYPE.REGION_CONTROLLER, *args, **kwargs)

    def delete(self):
        """Delete this region controller."""
        # Avoid circular dependency.
        from maasserver.models import RegionControllerProcess
        connections = RegionControllerProcess.objects.filter(
            region=self)

        if len(connections) != 0:
            raise ValidationError(
                "Unable to delete %s as it's currently running."
                % self.hostname)

        if self.node_type == NODE_TYPE.REGION_AND_RACK_CONTROLLER:
            # Node.as_self() returns a RackController object when the node is
            # a REGION_AND_RACK_CONTROLLER. Thus the API and websocket will
            # transition a REGION_AND_RACK_CONTROLLER to a REGION_CONTROLLER.
            self.node_type = NODE_TYPE.RACK_CONTROLLER
            self.save()
        elif self._was_probably_machine():
            self.node_type = NODE_TYPE.MACHINE
            self.save()
        else:
            super().delete()

    @inlineCallbacks
    def refresh(self):
        """Refresh the region controller."""
        # XXX ltrager 2016-05-25 - MAAS doesn't have an RPC method between
        # region controllers. If this method refreshes a foreign region
        # controller the foreign region controller will contain the running
        # region's hardware and networking information.
        if self.system_id != get_maas_id():
            raise NotImplementedError(
                'Can only refresh the running region controller')

        try:
            with NamedLock('refresh'):
                token = yield deferToDatabase(self._get_token_for_controller)
                yield deferToDatabase(self._signal_start_of_refresh)
                sys_info = yield deferToThread(get_sys_info)
                yield deferToDatabase(self._process_sys_info, sys_info)
                yield deferToThread(
                    refresh, self.system_id, token.consumer.key, token.key,
                    token.secret)
        except NamedLock.NotAvailable:
            # Refresh already running.
            pass


class Device(Node):
    """A non-installable node."""

    objects = DeviceManager()

    class Meta(DefaultMeta):
        proxy = True

    def __init__(self, *args, **kwargs):
        super(Device, self).__init__(
            node_type=NODE_TYPE.DEVICE, *args, **kwargs)

    def clean_architecture(self, prev):
        # Devices aren't required to have a defined architecture
        pass


class NodeGroupToRackController(CleanSave, Model):
    """Store some of the old NodeGroup data so we can migrate it when a rack
    controller is registered.
    """

    class Meta(DefaultMeta):
        """Inexplicably required."""

    # The uuid of the nodegroup from < 2.0
    uuid = CharField(max_length=36, null=False, blank=False, editable=True)

    # The subnet that the nodegroup is connected to. There can be multiple
    # rows for multiple subnets on a signal nodegroup
    subnet = ForeignKey('Subnet', null=False, blank=False, editable=True)
