#!/usr/bin/env python

# Update the base image that is used for devstack VMs.

# Copyright (C) 2011-2012 OpenStack LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
#
# See the License for the specific language governing permissions and
# limitations under the License.

import socket
import time
import traceback

import novaclient
from novaclient.v1_1 import client as Client
import paramiko
import statsd

from devstack_gate.sshclient import SSHClient
from devstack_gate import vmdatabase


def iterate_timeout(max_seconds, purpose):
    start = time.time()
    count = 0
    while (time.time() < start + max_seconds):
        count += 1
        yield count
        time.sleep(2)
    raise Exception("Timeout waiting for %s" % purpose)


def get_client(provider):
    args = [provider.nova_username, provider.nova_api_key,
            provider.nova_project_id, provider.nova_auth_url]
    kwargs = {}
    if provider.nova_service_type:
        kwargs['service_type'] = provider.nova_service_type
    if provider.nova_service_name:
        kwargs['service_name'] = provider.nova_service_name
    if provider.nova_service_region:
        kwargs['region_name'] = provider.nova_service_region
    client = Client(*args, **kwargs)
    return client

extension_cache = {}


def get_extensions(client):
    global extension_cache
    cache = extension_cache.get(client)
    if cache:
        return cache
    try:
        resp, body = client.client.get('/extensions')
        extensions = [x['alias'] for x in body['extensions']]
    except novaclient.exceptions.NotFound:
        extensions = []
    extension_cache[client] = extensions
    return extensions


def get_flavor(client, min_ram):
    flavors = [f for f in client.flavors.list() if f.ram >= min_ram]
    flavors.sort(lambda a, b: cmp(a.ram, b.ram))
    return flavors[0]


def get_public_ip(server, version=4):
    if 'os-floating-ips' in get_extensions(server.manager.api):
        print 'using floating ips'
        for addr in server.manager.api.floating_ips.list():
            print 'checking addr', addr
            if addr.instance_id == server.id:
                print 'found addr', addr
                return addr.ip
    print 'no floating ip, addresses:'
    print server.addresses
    for addr in server.addresses.get('public', []):
        if type(addr) == type(u''):  # Rackspace/openstack 1.0
            return addr
        if addr['version'] == version:  # Rackspace/openstack 1.1
            return addr['addr']
    for addr in server.addresses.get('private', []):
        # HP Cloud
        if addr['version'] == version and not addr['addr'].startswith('10.'):
            return addr['addr']
    return None


def add_public_ip(server):
    ip = server.manager.api.floating_ips.create()
    print "created floating ip", ip
    server.add_floating_ip(ip)
    for count in iterate_timeout(600, "ip to be added"):
        try:
            newip = ip.manager.get(ip.id)
        except Exception:
            print "Unable to get ip details, will retry"
            traceback.print_exc()
            continue

        if newip.instance_id == server.id:
            print 'ip has been added'
            return


def add_keypair(client, name):
    key = paramiko.RSAKey.generate(2048)
    public_key = key.get_name() + ' ' + key.get_base64()
    kp = client.keypairs.create(name, public_key)
    return key, kp


def wait_for_resource(wait_resource):
    last_progress = None
    last_status = None
    # It can take a _very_ long time for Rackspace 1.0 to save an image
    for count in iterate_timeout(21600, "waiting for %s" % wait_resource):
        try:
            resource = wait_resource.manager.get(wait_resource.id)
        except Exception:
            print "Unable to list resources, will retry"
            traceback.print_exc()
            continue

        if (last_progress != resource.progress
                or last_status != resource.status):
            print resource.status, resource.progress
        last_progress = resource.progress
        last_status = resource.status
        if resource.status == 'ACTIVE':
            return resource


def ssh_connect(ip, username, connect_kwargs={}, timeout=60):
    # HPcloud may return errno 111 for about 30 seconds after adding the IP
    for count in iterate_timeout(timeout, "ssh access"):
        try:
            client = SSHClient(ip, username, **connect_kwargs)
            break
        except socket.error, e:
            print "While testing ssh access:", e

    out = client.ssh("test ssh access", "echo access okay")
    if "access okay" in out:
        return client
    return None


def delete_server(server):
    try:
        if 'os-floating-ips' in get_extensions(server.manager.api):
            for addr in server.manager.api.floating_ips.list():
                if addr.instance_id == server.id:
                    server.remove_floating_ip(addr)
                    addr.delete()
    except Exception:
        print "Unable to remove floating IP"
        traceback.print_exc()

    try:
        if 'os-keypairs' in get_extensions(server.manager.api):
            for kp in server.manager.api.keypairs.list():
                if kp.name == server.key_name:
                    kp.delete()
    except Exception:
        print "Unable to delete keypair"
        traceback.print_exc()

    print "Deleting server", server.id
    server.delete()


def update_stats(provider):
    state_names = {
        vmdatabase.BUILDING: 'building',
        vmdatabase.READY: 'ready',
        vmdatabase.USED: 'used',
        vmdatabase.ERROR: 'error',
        vmdatabase.HOLD: 'hold',
        vmdatabase.DELETE: 'delete',
        }

    for base_image in provider.base_images:
        states = {
            vmdatabase.BUILDING: 0,
            vmdatabase.READY: 0,
            vmdatabase.USED: 0,
            vmdatabase.ERROR: 0,
            vmdatabase.HOLD: 0,
            vmdatabase.DELETE: 0,
            }
        for machine in base_image.machines:
            if machine.state not in states:
                continue
            states[machine.state] += 1
        if statsd.statsd:
            for state_id, count in states.items():
                key = 'devstack.pool.%s.%s.%s' % (
                    provider.name,
                    base_image.name,
                    state_names[state_id])
                statsd.statsd.gauge(key, count)

            key = 'devstack.pool.%s.%s.min_ready' % (
                provider.name,
                base_image.name)
            statsd.statsd.gauge(key, base_image.min_ready)

    if statsd.statsd:
        key = 'devstack.pool.%s.max_servers' % provider.name
        statsd.statsd.gauge(key, provider.max_servers)
