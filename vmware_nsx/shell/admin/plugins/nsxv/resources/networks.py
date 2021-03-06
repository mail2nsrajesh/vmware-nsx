# Copyright 2016 VMware, Inc.  All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_log import log as logging
from oslo_serialization import jsonutils
import re
import xml.etree.ElementTree as et

from neutron_lib.callbacks import registry
from neutron_lib import context

from vmware_nsx.db import db as nsx_db
from vmware_nsx.plugins.nsx_v.vshield.common import exceptions
from vmware_nsx.shell.admin.plugins.common import constants
from vmware_nsx.shell.admin.plugins.common import formatters
from vmware_nsx.shell.admin.plugins.common import utils as admin_utils
from vmware_nsx.shell.admin.plugins.nsxv.resources import utils as utils
from vmware_nsx.shell import resources as shell

LOG = logging.getLogger(__name__)
nsxv = utils.get_nsxv_client()
network_types = ['Network', 'VirtualWire', 'DistributedVirtualPortgroup']
PORTGROUP_PREFIX = 'dvportgroup'


def get_networks_from_backend():
    nsxv = utils.get_nsxv_client()
    so_list = nsxv.get_scoping_objects()
    return et.fromstring(so_list)


def get_networks():
    """Create an array of all the backend networks and their data
    """
    root = get_networks_from_backend()
    networks = []
    for obj in root.iter('object'):
        if obj.find('objectTypeName').text in network_types:
            networks.append({'type': obj.find('objectTypeName').text,
                             'moref': obj.find('objectId').text,
                             'name': obj.find('name').text})
    return networks


def get_networks_name_map():
    """Create a dictionary mapping moref->backend name
    """
    root = get_networks_from_backend()
    networks = {}
    for obj in root.iter('object'):
        if obj.find('objectTypeName').text in network_types:
            networks[obj.find('objectId').text] = obj.find('name').text
    return networks


@admin_utils.output_header
def neutron_list_networks(resource, event, trigger,
                          **kwargs):
    LOG.info(formatters.output_formatter(constants.NETWORKS, get_networks(),
                                         ['type', 'moref', 'name']))


@admin_utils.output_header
def nsx_update_switch(resource, event, trigger, **kwargs):
    nsxv = utils.get_nsxv_client()
    if not kwargs.get('property'):
        LOG.error("Need to specify dvs-id parameter and "
                  "attribute to update. Add --property dvs-id=<dvs-id> "
                  "--property teamingpolicy=<policy>")
        return
    properties = admin_utils.parse_multi_keyval_opt(kwargs['property'])
    dvs_id = properties.get('dvs-id')
    if not dvs_id:
        LOG.error("Need to specify dvs-id. "
                  "Add --property dvs-id=<dvs-id>")
        return
    try:
        h, switch = nsxv.get_vdn_switch(dvs_id)
    except exceptions.ResourceNotFound:
        LOG.error("DVS %s not found", dvs_id)
        return
    supported_policies = ['ETHER_CHANNEL', 'LOADBALANCE_LOADBASED',
                          'LOADBALANCE_SRCID', 'LOADBALANCE_SRCMAC',
                          'FAILOVER_ORDER', 'LACP_ACTIVE', 'LACP_PASSIVE',
                          'LACP_V2']
    policy = properties.get('teamingpolicy')
    if policy in supported_policies:
        if switch['teamingPolicy'] == policy:
            LOG.info("Policy already set!")
            return
        LOG.info("Updating NSXv switch %(dvs)s teaming policy to "
                 "%(policy)s", {'dvs': dvs_id, 'policy': policy})
        switch['teamingPolicy'] = policy
        try:
            switch = nsxv.update_vdn_switch(switch)
        except exceptions.VcnsApiException as e:
            desc = jsonutils.loads(e.response)
            details = desc.get('details')
            if details.startswith("No enum constant"):
                LOG.error("Unknown teaming policy %s", policy)
            else:
                LOG.error("Unexpected error occurred: %s", details)
            return

        LOG.info("Switch value after update: %s", switch)
    else:
        LOG.info("Current switch value is: %s", switch)
        LOG.error("Invalid teaming policy. "
                  "Add --property teamingpolicy=<policy>")
        LOG.error("Possible values: %s", ', '.join(supported_policies))


@admin_utils.output_header
def list_missing_networks(resource, event, trigger, **kwargs):
    """List the neutron networks which are missing the backend moref
    """
    # get the neutron-nsx networks mapping from DB
    admin_context = context.get_admin_context()
    mappings = nsx_db.get_nsx_networks_mapping(admin_context.session)
    # get the list of backend networks:
    backend_networks = get_networks_name_map()
    missing_networks = []

    # For each neutron network - check if there is a matching backend network
    for entry in mappings:
        nsx_id = entry['nsx_id']
        dvs_id = entry['dvs_id']
        if nsx_id not in backend_networks.keys():
            missing_networks.append({'neutron_id': entry['neutron_id'],
                                     'moref': nsx_id,
                                     'dvs_id': dvs_id})
        elif dvs_id:
            netname = backend_networks[nsx_id]
            if not netname.startswith(dvs_id):
                missing_networks.append({'neutron_id': entry['neutron_id'],
                                         'moref': nsx_id,
                                         'dvs_id': dvs_id})

    LOG.info(formatters.output_formatter(constants.MISSING_NETWORKS,
                                         missing_networks,
                                         ['neutron_id', 'moref', 'dvs_id']))


@admin_utils.output_header
def list_orphaned_networks(resource, event, trigger, **kwargs):
    """List the NSX networks which are missing the neutron DB
    """
    admin_context = context.get_admin_context()
    missing_networks = []

    # get the list of backend networks:
    backend_networks = get_networks()
    for net in backend_networks:
        moref = net['moref']
        backend_name = net['name']
        if backend_name.startswith('edge-') or net['type'] == 'Network':
            # This is not a neutron network
            continue
        # get the list of neutron networks with this moref
        neutron_networks = nsx_db.get_nsx_network_mapping_for_nsx_id(
            admin_context.session, moref)
        if not neutron_networks:
            # no network found for this moref
            missing_networks.append(net)

        elif moref.startswith(PORTGROUP_PREFIX):
            # This is a VLAN network. Also verify that the DVS Id matches
            for entry in neutron_networks:
                if (not entry['dvs_id'] or
                    backend_name.startswith(entry['dvs_id'])):
                    found = True
            # this moref & dvs-id does not appear in the DB
            if not found:
                missing_networks.append(net)

    LOG.info(formatters.output_formatter(constants.ORPHANED_NETWORKS,
                                         missing_networks,
                                         ['type', 'moref', 'name']))


def get_dvs_id_from_backend_name(backend_name):
    reg = re.search(r"^dvs-\d*", backend_name)
    if reg:
        return reg.group(0)


@admin_utils.output_header
def delete_backend_network(resource, event, trigger, **kwargs):
    """Delete a backend network by its moref
    """
    errmsg = ("Need to specify moref property. Add --property moref=<moref>")
    if not kwargs.get('property'):
        LOG.error("%s", errmsg)
        return
    properties = admin_utils.parse_multi_keyval_opt(kwargs['property'])
    moref = properties.get('moref')
    if not moref:
        LOG.error("%s", errmsg)
        return

    backend_name = get_networks_name_map().get(moref)
    if not backend_name:
        LOG.error("Failed to find the backend network %(moref)s",
                  {'moref': moref})
        return

    # Note: in case the backend network is attached to other backend objects,
    # like VM, the deleting may fail and through an exception

    nsxv = utils.get_nsxv_client()
    if moref.startswith(PORTGROUP_PREFIX):
        # get the dvs id from the backend name:
        dvs_id = get_dvs_id_from_backend_name(backend_name)
        if not dvs_id:
            LOG.error("Failed to find the DVS id of backend network "
                      "%(moref)s", {'moref': moref})
        else:
            try:
                nsxv.delete_port_group(dvs_id, moref)
            except Exception as e:
                LOG.error("Failed to delete backend network %(moref)s : "
                          "%(e)s", {'moref': moref, 'e': e})
            else:
                LOG.info("Backend network %(moref)s was deleted",
                         {'moref': moref})
    else:
        # Virtual wire
        try:
            nsxv.delete_virtual_wire(moref)
        except Exception as e:
            LOG.error("Failed to delete backend network %(moref)s : "
                      "%(e)s", {'moref': moref, 'e': e})
        else:
            LOG.info("Backend network %(moref)s was deleted",
                     {'moref': moref})


registry.subscribe(neutron_list_networks,
                   constants.NETWORKS,
                   shell.Operations.LIST.value)
registry.subscribe(nsx_update_switch,
                   constants.NETWORKS,
                   shell.Operations.NSX_UPDATE.value)
registry.subscribe(list_missing_networks,
                   constants.MISSING_NETWORKS,
                   shell.Operations.LIST.value)
registry.subscribe(list_orphaned_networks,
                   constants.ORPHANED_NETWORKS,
                   shell.Operations.LIST.value)
registry.subscribe(delete_backend_network,
                   constants.ORPHANED_NETWORKS,
                   shell.Operations.NSX_CLEAN.value)
