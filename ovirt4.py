#!/usr/bin/env python
# -*- coding:utf-8 _*_

"""
oVirt dynamic inventory script
=================================

Generates dynamic inventory file for oVirt.

Inspired by: https://github.com/ansible/ansible/blob/devel/contrib/inventory/ovirt4.py

Script will return following attributes for each virtual machine:
 - id
 - name
 - host
 - cluster
 - status
 - description
 - fqdn
 - os_type
 - template
 - tags
 - statistics
 - devices

When run in --list mode, virtual machines are grouped by the following categories:
 - cluster
 - tag
 - status

 Note: If there is some virtual machine which has has more tags it will be in both tag
       records.

Examples:

  # Execute update of system on webserver virtual machine:

    $ ansible -i contrib/inventory/ovirt4.py webserver -m yum -a "name=* state=latest"

  # Get webserver virtual machine information:

    $ contrib/inventory/ovirt4.py --host webserver

Author: Fabien Dupont (@fdupont-redhat)
"""

import sys
import os
import argparse
import re
import six
from six.moves import configparser

try:
    import json
except ImportError:
    import simplejson as json

try:
    import ovirtsdk4 as sdk
    import ovirtsdk4.types as otypes
except ImportError:
    print('oVirt inventory script requires ovirt-engine-sdk-python >= 4.0.0')
    sys.exit(1)

class Ovirt4Inventory(object):

    def __init__(self):
        ''' Main execution path '''

        # Parse command line arguments and read settings
        self.args = self.parse_cli_args()
        self.settings = self.read_settings()

        # Connect to oVirt 4 API
        self.connect()

        self.build_inventory()

        # Data to print
        if self.args.host:
            data_to_print = self.json_format_dict(self.inventory['_meta']['hostvars'][self.args.host], True)
        elif self.args.list:
            data_to_print = self.json_format_dict(self.inventory, True)

        # Disconnect from oVirt 4 API
        self.disconnect()

        print(data_to_print)

    def read_settings(self):
        ''' Reads the settings from the ovirt.ini file '''

        settings = {}

        default_path = os.path.join(
            os.path.dirname(os.path.realpath(__file__)),
            'ovirt.ini'
        )
        config_path = os.environ.get('OVIRT_INI_PATH', default_path)

        config = configparser.SafeConfigParser(
            defaults={
                'ovirt_url': os.environ.get('OVIRT_URL'),
                'ovirt_username': os.environ.get('OVIRT_USERNAME'),
                'ovirt_password': os.environ.get('OVIRT_PASSWORD'),
                'ovirt_ca_file': os.environ.get('OVIRT_CA_FILE')
            }
        )
        if not config.has_section('ovirt'):
            config.add_section('ovirt')
        config.read(config_path)

        settings['ovirt'] = {
            'url': config.get('ovirt', 'ovirt_url'),
            'username': config.get('ovirt', 'ovirt_username'),
            'password': config.get('ovirt', 'ovirt_password'),
            'ca_file': config.get('ovirt', 'ovirt_ca_file')
        }

        if config.has_section('format'):
            settings['format'] = {}
            settings['format']['replace_dash_in_groups'] = config.getboolean('format', 'replace_dash_in_groups')

        return settings

    def parse_cli_args(self):
        ''' Command line argument processing '''

        parser = argparse.ArgumentParser(description='Produce an Ansible Inventory file based on oVirt 4')
        parser.add_argument('--list', action='store_true', default=True,
                            help='List instances (default: True)')
        parser.add_argument('--host', help='Get all the variables about a specific host')
        parser.add_argument('--pretty', action='store_true', default=False,
                            help='Pretty format (default: False)')
        return parser.parse_args()

    def connect(self):
        ''' Create connection to oVirt 4 API '''

        connection = sdk.Connection(
            url=self.settings['ovirt']['url'],
            username=self.settings['ovirt']['username'],
            password=self.settings['ovirt']['password'],
            ca_file=self.settings['ovirt']['ca_file']
        )
        self.connection = connection
        self.system_service = self.connection.system_service()
        self.services = {
            'data_centers': self.system_service.data_centers_service(),
            'clusters': self.system_service.clusters_service(),
            'hosts': self.system_service.hosts_service(),
            'vms': self.system_service.vms_service()
        }

    def disconnect(self):
        self.connection.close()

    def get_dict_from_object(self, obj, prefix=''):
        object_vars = {}

        for key in vars(obj):
            value = getattr(obj, key)
            key = self.to_safe(prefix + key)

            if isinstance(value, (int, bool)):
                object_vars[key] = value
            elif isinstance(value, six.string_types):
                object_vars[key] = value.strip()
            elif value is None:
#                object_vars[key] = ''
                object_vars[key] = None
            else:
                pass

        return object_vars

    def get_data_centers(self):
        data_centers = {}
        for data_center in self.services['data_centers'].list():
            data_centers[data_center.id] = self.get_dict_from_object(data_center)
        return data_centers

    def get_clusters(self):
        clusters = {}
        for cluster in self.services['clusters'].list():
            clusters[cluster.id] = self.get_dict_from_object(cluster)
            clusters[cluster.id]['data_center'] = self.data_centers[cluster.data_center.id]
        return clusters

    def get_affinity_groups(self, cluster):
        affinity_groups = {}
        for affinity_group in self.services['clusters'].cluster_service(cluster.id).affinity_groups_service().list():
            affinity_groups[affinity_group.id] = self.get_dict_from_object(affinity_group)
        return affinity_groups

    def get_hosts(self):
        hosts = {}
        for host in self.services['hosts'].list():
            host_service = self.services['hosts'].host_service(host.id)
            hosts[host.id] = self.get_dict_from_object(host)
            hosts[host.id]['status'] = str(host.status)
            hosts[host.id]['cluster'] = self.clusters[host.cluster.id]
            hosts[host.id]['tags'] = [tag.name for tag in host_service.tags_service().list()]
        return hosts

    def get_vms(self):
        vms = {}
        for vm in self.services['vms'].list():
            vm_service = self.services['vms'].vm_service(vm.id)
            vms[vm.id] = self.get_dict_from_object(vm)
            vms[vm.id]['host'] = vm.host.name if vm.host else None
            vms[vm.id]['status'] = str(vm.status)
            vms[vm.id]['os_type'] = vm.os.type
            vms[vm.id]['template'] = vm.template.name
            vms[vm.id]['nics'] = {}
            for device in vm_service.reported_devices_service().list():
                nic = {}
                if device.mac:
                    nic['mac_address'] = device.mac.address
                if device.ips:
                    nic['ip_addresses'] = [ip.address for ip in device.ips]
                vms[vm.id]['nics'][device.name] = nic
            vms[vm.id]['tags'] = [tag.name for tag in vm_service.tags_service().list()]
            vms[vm.id]['statistics'] = dict(
                (stat.name, stat.values[0].datum) for stat in vm_service.statistics_service().list()
            )
            vms[vm.id]['affinity_labels'] = [label.name for label in vm_service.affinity_labels_service().list()]
            vms[vm.id]['affinity_groups'] = [
                group.name for group in self.get_affinity_groups(vm.cluster)
                if vm.name in [vm.name for vm in group.vms]
            ]
            vms[vm.id]['cluster'] = self.clusters[vm.cluster.id]
        return vms

    def add_host_to_group(self, host, group):
        if not group in self.inventory:
            self.inventory[group] = { 'hosts': [] }
        self.inventory[group]['hosts'].append(host)

    def build_inventory(self):
        ''' Generates an inventory as a dict following relationships '''
        self.inventory = {"_meta": {"hostvars": {}},"all": {"children": ["ovirt_hosts", "ovirt_vms"]}, "ovirt_hosts": { "hosts": [] }, "ovirt_vms": { "hosts": [] }}

        self.data_centers = self.get_data_centers()
        self.clusters = self.get_clusters()
        self.hosts = self.get_hosts()
        self.vms = self.get_vms()

        for key, value in self.hosts.iteritems():
            host = {}
            host['ovirt'] = value
            if not value['address'] == value['id']:
                host['ansible_host'] = value['address']
            self.inventory['_meta']['hostvars'][value['name']] = host
            self.inventory['ovirt_hosts']['hosts'].append(value['name'])
            self.add_host_to_group(value['name'], 'ovirt_data_center_%s' % value['cluster']['data_center']['name'])
            self.add_host_to_group(value['name'], 'ovirt_cluster_%s' % value['cluster']['name'])

        for key, value in self.vms.iteritems():
            vm = {}
            vm['ovirt'] = value
            if value['nics']:
                vm['ansible_host'] = [nic['ip_addresses'][0] for nic in value['nics'].values()][0]
            self.inventory['_meta']['hostvars'][value['name']] = vm
            self.inventory['ovirt_vms']['hosts'].append(value['name'])
            self.add_host_to_group(value['name'], 'ovirt_data_center_%s' % value['cluster']['data_center']['name'])
            self.add_host_to_group(value['name'], 'ovirt_cluster_%s' % value['cluster']['name'])
            for tag in value['tags']:
                self.add_host_to_group(value['name'], 'ovirt_tag_%s' % tag)
            if value['host']:
                self.add_host_to_group(value['name'], 'ovirt_host_%s' % value['host'])

    def to_safe(self, word):
        ''' Converts 'bad' characters in a string to underscores so they can be
        used as Ansible groups '''
        regex = r"[^A-Za-z0-9\_"
        if not self.settings['format']['replace_dash_in_groups']:
            regex += r"\-"
        return re.sub(r"^\_", "", re.sub(regex + "]", "_", word))

    def json_format_dict(self, data, pretty=False):
        ''' Converts a dict to a JSON object and dumps it as a formatted
        string '''
        if pretty:
            return json.dumps(data, sort_keys=True, indent=2)
        else:
            return json.dumps(data)


if __name__ == '__main__':
    Ovirt4Inventory()
