#!/usr/bin/env python
# StormBench 1.0 (C) Kenneth Falck <kennu@iki.fi> 2012.
# See http://github.com/kennu/stormbench for more information.
# See LICENSE for copyright information (BSD license).
# Required Python packages: boto redis (pip install boto redis)
from __future__ import print_function
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from boto.ec2 import connect_to_region
import redis
import argparse
import logging
import time
import sys
import re

SSH_PORT = 22
REDIS_PORT = 6379
ALL_HOSTS = '0.0.0.0/0'

# This list is from http://cloud-images.ubuntu.com/releases/precise/release/ (32-bit EBS)
UBUNTU_IMAGES = {
    'ap-northeast-1': 'ami-20ad1221',
    'ap-southeast-1': 'ami-ea8acab8',
    'eu-west-1': 'ami-c7aaabb3',
    'sa-east-1': 'ami-cc19c0d1',
    'us-east-1': 'ami-3b4ff252',
    'us-west-1': 'ami-03153246',
    'us-west-2': 'ami-8c109ebc',
}

COMMON_USER_DATA = '#!/bin/sh\n' + \
    'apt-get update >>/tmp/stormbench.log 2>&1\n' + \
    'DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -q -y >>/tmp/stormbench.log 2>&1\n' + \
    'DEBIAN_FRONTEND=noninteractive apt-get install -q -y apache2-utils redis-server >>/tmp/stormbench.log 2>&1\n' + \
    'echo "StormBench initialization done." >> /tmp/stormbench.log\n'

def make_server_user_data():
    """
    Generate user data for a Redis server instance.
    """
    # Additional configuration to open Redis server to world
    return COMMON_USER_DATA + \
        'grep -q "bind 0.0.0.0" /etc/redis/redis.conf || echo "bind 0.0.0.0" >> /etc/redis/redis.conf\n' + \
        '/etc/init.d/redis-server restart\n'
    

def make_image_user_data(server_address):
    """
    Generate user data for a temporary image instance.
    This will register the client on the Redis server.
    """
    return COMMON_USER_DATA + \
        '/usr/bin/redis-cli -h "%s" hset clients "`ec2metadata --instance-id`" "`ec2metadata --local-hostname`" >> /tmp/stormbench.log 2>&1\n' % (server_address)

def make_client_user_data(server_address, ab_command_line):
    """
    Generate user data for a client instance.
    This will register the client on the Redis server, wait for the trigger, perform the ApacheBench run and then submit the results.
    Note: Uses nonstandard %N (nanosecond) field for date.
    """
    return COMMON_USER_DATA + \
        '/usr/bin/redis-cli -h "%s" hset clients "`ec2metadata --instance-id`" "`ec2metadata --local-hostname`" >> /tmp/stormbench.log 2>&1\n' % (server_address) + \
        'while ! /usr/bin/redis-cli -h "%s" exists trigger | grep -q 1; do sleep 1; done\n' % (server_address) + \
        'date +"Start-Time: %Y-%m-%d %H:%M:%S %N" > /tmp/ab.log\n' + \
        ab_command_line + ' >> /tmp/ab.log 2>&1\n' + \
        'date +"End-Time: %Y-%m-%d %H:%M:%S %N" >> /tmp/ab.log\n' + \
        '/usr/bin/redis-cli -h "%s" hset results "`ec2metadata --instance-id`" "`cat /tmp/ab.log`" >> /tmp/stormbench.log 2>&1\n' % (server_address)

def create_security_group(ec2_conn, args):
    """
    Create the StormBench security group with permission for TCP port 2181.
    If the group already exists, this won't do anything.
    """
    if args.group != 'stormbench':
        print('Not creating nondefault security group %s' % args.group)
        return
    try:
        if ec2_conn.get_all_security_groups(groupnames=[args.group]):
            print('Security group %s exists, good.' % args.group)
            return
    except:
        # Expecting a EC2ResponseError of group not found
        pass
    print('Creating security group %s...' % args.group)
    group = ec2_conn.create_security_group(args.group, 'StormBench access group')
    group.authorize(ip_protocol='tcp', from_port=SSH_PORT, to_port=SSH_PORT, cidr_ip=ALL_HOSTS)
    group.authorize(ip_protocol='tcp', from_port=REDIS_PORT, to_port=REDIS_PORT, cidr_ip=ALL_HOSTS)

def terminate_instances(ec2_conn, instances):
    if not instances:
        return
    print('Terminating instances: %s..' % (' '.join([instance.id for instance in instances])), end='')
    sys.stdout.flush()
    for instance in instances:
        instance.terminate()
    while [instance for instance in instances if instance.state != 'terminated']:
        print('.', end='')
        sys.stdout.flush()
        time.sleep(1)
        for instance in instances:
            if instance.state != 'terminated':
                instance.update()
    print(' All terminated.')

def launch_redis_server(ec2_conn, args):
    """
    Launch a central Redis server that other clients can connect to.
    Returns the public DNS name of the server, even if it was already running.
    """
    # Check if the server is already running (with tag StormBenchRole:Server)
    reservations = ec2_conn.get_all_instances(filters={'tag:StormBenchRole':'Server'})
    if reservations:
        running = False
        address = None
        for reservation in reservations:
            for instance in reservation.instances:
                if instance.state != 'terminated' and instance.state != 'shutting-down':
                    running = True
                    address = instance.public_dns_name
                    print('Redis server instance %s %s at %s %s.' % (instance.id, instance.state, instance.public_dns_name, instance.private_dns_name))
        if running:
            return address
    tag = args.prefix + '-server'
    user_data = make_server_user_data()
    print('Launching Redis server instance...')
    reservation = ec2_conn.run_instances(image_id=args.ami, instance_type=args.type, user_data=user_data, key_name=args.keypair, security_groups=[args.group])
    instance = reservation.instances[0]
    # It can take a moment for the instance id to be available
    time.sleep(10)
    print('Tagging server instance %s as %s' % (instance.id, tag))
    instance.add_tag('StormBench', 'True')
    instance.add_tag('StormBenchRole', 'Server')
    instance.add_tag('Name', tag)
    print('Waiting for server instance %s to start up..' % instance.id, end='')
    sys.stdout.flush()
    while instance.state != 'running':
        print('.', end='')
        sys.stdout.flush()
        instance.update()
        time.sleep(5)
    print(' %s %s %s' % (instance.state, instance.public_dns_name, instance.private_dns_name))
    return instance.public_dns_name

def connect_to_redis_server(server_address):
    print('Connecting to Redis server at %s...' % server_address, end='')
    sys.stdout.flush()
    redis_client = redis.StrictRedis(host=server_address)
    while True:
        try:
            redis_client.incr('connections')
            break
        except redis.exceptions.ConnectionError:
            print('.', end='')
            sys.stdout.flush()
            time.sleep(5)
    print(' Connected.')
    return redis_client

def reset_redis_data(redis_client):
    redis_client.delete('clients', 'results', 'trigger')

def terminate_redis_server(ec2_conn, args):
    """
    Terminate a previously launched Redis server (if it exists).
    """
    instances_to_terminate = []
    reservations = ec2_conn.get_all_instances(filters={'tag:StormBenchRole':'Server'})
    if reservations:
        for reservation in reservations:
            for instance in reservation.instances:
                if instance.state != 'terminated' and instance.state != 'shutting-down':
                    print('Terminating Redis server instance %s %s %s %s' % (instance.id, instance.tags.get('Name', '(unnamed)'), instance.public_dns_name, instance.private_dns_name))
                    instances_to_terminate.append(instance)
    if not instances_to_terminate:
        print('No Redis server instances to terminate.')
    else:
        terminate_instances(ec2_conn, instances_to_terminate)

def wait_for_redis_clients(redis_client, client_instance_ids):
    """
    Wait for the specified clients to register on the Redis server.
    """
    print('Waiting for %d client(s) to register on Redis server...' % len(client_instance_ids))
    remaining_clients = list(client_instance_ids)
    while remaining_clients:
        for client_instance_id in remaining_clients:
            if redis_client.hexists('clients', client_instance_id):
                remaining_clients.remove(client_instance_id)
                print('\r%d client(s) remaining' % len(remaining_clients))
                sys.stdout.flush()
        if remaining_clients:
            time.sleep(5)
    print('All %d client(s) registered.' % len(client_instance_ids))

def parse_result(text):
    return dict([[f.strip() for f in line.split(':', 1)] for line in text.replace('\r', '').split('\n') if line.find(':') > 0])

def wait_for_redis_results(redis_client, client_instance_ids):
    """
    Wait for the specified clients to submit results to the Redis server.
    """
    print('Waiting for %d client(s) to submit results to Redis server...' % len(client_instance_ids))
    results = {}
    remaining_clients = list(client_instance_ids)
    while remaining_clients:
        for client_instance_id in remaining_clients:
            if redis_client.hexists('results', client_instance_id):
                results[client_instance_id] = parse_result(redis_client.hget('results', client_instance_id))
                remaining_clients.remove(client_instance_id)
                print('\r%d client(s) remaining' % len(remaining_clients))
                sys.stdout.flush()
        if remaining_clients:
            time.sleep(5)
    print('All %d client(s) submitted results.' % len(client_instance_ids))
    return results

def print_results(results):
    total_bit_rate = 0
    valid_result_count = 0
    invalid_result_count = 0
    for key, result in results.items():
        start_time_text = result.get('Start-Time', '')
        end_time_text = result.get('End-Time', '')
        rate_text = result.get('Transfer rate', '') # Kbytes/s
        if start_time_text and end_time_text and ' ' in rate_text:
            # Parse result fields in more detail
            start_time = datetime.strptime(start_time_text[:26], '%Y-%m-%d %H:%M:%S %f') # 2012-12-31 23:59:59 999999999
            end_time = datetime.strptime(end_time_text[:26], '%Y-%m-%d %H:%M:%S %f')
            kbyte_rate_text, _rest = rate_text.split(' ', 1)
            bit_rate = float(kbyte_rate_text) * 1024 * 8
            total_bit_rate += bit_rate
            print('%s: %.2f Mbit/s (%s) %s - %s' % (key, bit_rate/1024/1024, kbyte_rate_text, start_time.isoformat(), end_time.isoformat()))
            valid_result_count += 1
        else:
            invalid_result_count += 1
    print('Total transfer rate: %.2f Mbit/s' % (total_bit_rate/1024/1024))
    print('Average transfer rate: %.2f Mbit/s' % (total_bit_rate/1024/1024/valid_result_count if valid_result_count else 0.0))
    print('%d valid result(s)' % (valid_result_count))
    print('%d invalid result(s)' % (invalid_result_count))

def auto_choose_ami(ec2_conn, args):
    """
    If an image has been created, use it. Otherwise use a default Ubuntu image.
    """
    if args.ami:
        return
    # Try to find existing image.
    prev_name = ''
    for image in ec2_conn.get_all_images(owners=['self'], filters={'tag:StormBench':'True'}):
        if image.tags['StormBench'] == 'True':
            # Find image with latest name (sorted by date suffix)
            if image.name > prev_name:
                args.ami = image.id
                prev_name = image.name
    if args.ami:
        print('Using custom AMI image %s %s' % (args.ami, prev_name))
        return
    # Choose default AMI based on region
    args.ami = UBUNTU_IMAGES[args.region]
    print('Using Ubuntu AMI image %s' % args.ami)

def startserver(args):
    if args.dry:
        return
    ec2_conn = connect_to_region(args.region, aws_access_key_id=args.key, aws_secret_access_key=args.secret)
    auto_choose_ami(ec2_conn, args)
    create_security_group(ec2_conn, args)
    server_address = launch_redis_server(ec2_conn, args)
    # Connect to the newly launched server and wait for connection.
    redis_client = connect_to_redis_server(server_address)
    reset_redis_data(redis_client)

def stopserver(args):
    if args.dry:
        return
    ec2_conn = connect_to_region(args.region, aws_access_key_id=args.key, aws_secret_access_key=args.secret)
    terminate_redis_server(ec2_conn, args)

def createimage(args):
    now = datetime.now().strftime('%Y%m%d-%H%M%S')
    tag = args.prefix + '-' + now
    temp_tag = args.prefix + '-image'
    print('Creating new AMI image...')
    print('    Account: %s' % args.key)
    print('    Region: %s' % args.region)
    print('    Instance type: %s' % args.type)
    print('    AMI image name/tag: %s' % tag)
    print('    Temporary instance tag: %s' % temp_tag)
    print('    Temporary instance keypair: %s' % args.keypair)
    print('    Temporary instance security group: %s' % args.group)
    if args.dry:
        return
    ec2_conn = connect_to_region(args.region, aws_access_key_id=args.key, aws_secret_access_key=args.secret)
    auto_choose_ami(ec2_conn, args)
    create_security_group(ec2_conn, args)
    server_address = launch_redis_server(ec2_conn, args)
    
    # Connect to the newly launched server and wait for connection.
    redis_client = connect_to_redis_server(server_address)
    reset_redis_data(redis_client)
    
    # Now actually start the temporary instance for image creation.
    print('Starting image creation...')
    user_data = make_image_user_data(server_address)
    reservation = ec2_conn.run_instances(image_id=args.ami, instance_type=args.type, user_data=user_data, key_name=args.keypair, security_groups=[args.group])
    # There can be a delay before AWS knows about the new instance ID
    time.sleep(10)
    instance = reservation.instances[0]
    print('Tagging temporary instance %s as %s' % (instance.id, temp_tag))
    instance.add_tag('StormBench', 'True')
    instance.add_tag('StormBenchRole', 'Temporary')
    instance.add_tag('Name', temp_tag)
    print('Waiting for temporary instance %s to start..' % (instance.id), end='')
    sys.stdout.flush()
    while instance.state != 'running':
        print('.', end='')
        sys.stdout.flush()
        instance.update()
        time.sleep(5)
    print(' %s %s %s' % (instance.state, instance.public_dns_name, instance.private_dns_name))
    
    # Wait for the client to register on the Redis server.
    wait_for_redis_clients(redis_client, [instance.id])
    
    # Now we can create the AMI image.
    image_id = ec2_conn.create_image(instance.id, name=tag, description=tag)
    print('Creating AMI image %s..' % image_id, end='')
    # There can be a delay before AWS knows about the new image ID
    time.sleep(10)
    sys.stdout.flush()
    image = ec2_conn.get_image(image_id)
    image.add_tag('Name', tag)
    image.add_tag('StormBench', 'True')
    while image.state != 'available':
        print('.', end='')
        sys.stdout.flush()
        image.update()
        time.sleep(5)
    print(' Image created.')
    snapshot_id = image.block_device_mapping.get('/dev/sda1', None)
    if snapshot_id:
        snapshot_id = snapshot_id.snapshot_id
    if snapshot_id:
        print('Tagging AMI snapshot %s...' % snapshot_id)
        for snapshot in ec2_conn.get_all_snapshots(snapshot_ids=[snapshot_id]):
            snapshot.add_tag('Name', tag)
            snapshot.add_tag('StormBench', 'True')
    
    # All done, we can terminate the Redis server and the instances.
    print('Terminating temporary instance %s' % (instance.id))
    terminate_instances(ec2_conn, [instance])
    terminate_redis_server(ec2_conn, args)
    print('Ready.')

def status(args):
    print('Resources currently used by StormBench on EC2:')
    n_instances = 0
    n_images = 0
    n_snapshots = 0
    n_groups = 0
    ec2_conn = connect_to_region(args.region, aws_access_key_id=args.key, aws_secret_access_key=args.secret)
    for reservation in ec2_conn.get_all_instances(filters={'tag:StormBench':'True'}):
        for instance in reservation.instances:
            if instance.tags['StormBench'] == 'True' and instance.state != 'terminated':
                print('Instance %s %s %s %s %s %s' % (instance.id, instance.tags.get('Name', '(unnamed)'), instance.instance_type, instance.state, instance.public_dns_name, instance.private_dns_name))
                n_instances += 1
    for image in ec2_conn.get_all_images(owners=['self'], filters={'tag:StormBench':'True'}):
        if image.tags['StormBench'] == 'True':
            print('Image %s %s %s' % (image.id, image.name, image.tags.get('Name', '(unnamed)')))
            n_images += 1
    for snapshot in ec2_conn.get_all_snapshots(filters={'tag:StormBench':'True'}):
        if image.tags['StormBench'] == 'True':
            print('Snapshot %s %s' % (image.id, image.tags.get('Name', '(unnamed)')))
            n_snapshots += 1
    try:
        for group in ec2_conn.get_all_security_groups(groupnames=[args.group]):
            print('Security group %s' % (group.name))
            n_groups += 1
    except:
        # Expecting EC2ResponseError if group doesn't exist
        pass
    print('Total %d instance(s), %d image(s), %d snapshot(s) and %d security group(s).' % (n_instances, n_images, n_snapshots, n_groups))

def cleanup(args):
    print('Scanning for StormBench instances and images...')
    instances_to_terminate = []
    images_to_delete = []
    groups_to_delete = []
    ec2_conn = connect_to_region(args.region, aws_access_key_id=args.key, aws_secret_access_key=args.secret)
    latest_image = None
    prev_name = ''
    for reservation in ec2_conn.get_all_instances(filters={'tag:StormBench':'True'}):
        for instance in reservation.instances:
            if instance.tags['StormBench'] == 'True' and instance.state != 'terminated' and instance.state != 'shutting-down':
                instances_to_terminate.append(instance)
    for image in ec2_conn.get_all_images(owners=['self'], filters={'tag:StormBench':'True'}):
        if image.tags['StormBench'] == 'True':
            images_to_delete.append(image)
            if image.name > prev_name:
                latest_image = image
                prev_name = image.name
    if latest_image and not args.full:
        # Keep the latest image
        print('Keeping latest AMI image %s %s' % (latest_image.id, latest_image.name))
        images_to_delete.remove(latest_image)
    try:
        for group in ec2_conn.get_all_security_groups(groupnames=[args.group]):
            groups_to_delete.append(group)
    except:
        # Expecting EC2ResponseError if group doesn't exist
        pass
    if not instances_to_terminate and not images_to_delete and not groups_to_delete:
        print('Nothing to clean up.')
        return
    for instance in instances_to_terminate:
        print('Terminating instance %s %s %s %s %s %s' % (instance.id, instance.tags.get('Name', '(unnamed)'), instance.instance_type, instance.state, instance.public_dns_name, instance.private_dns_name))
    for image in images_to_delete:
        print('Deleting image %s %s %s' % (image.id, image.name, image.tags.get('Name', '(unnamed)')))
    for group in groups_to_delete:
        print('Deleting security group %s' % (group.name))
    print('About to terminate %d instance(s), delete %d image(s) and delete %d security group(s).' % (len(instances_to_terminate), len(images_to_delete), len(groups_to_delete)))
    print('Do you want to continue? [Ny]', end='')
    sys.stdout.flush()
    answer = sys.stdin.readline().strip()
    if answer not in ('y', 'Y'):
        print('Aborted.')
        return
    print('Cleaning up now...')
    for instance in instances_to_terminate:
        print('Terminating instance %s %s' % (instance.id, instance.tags.get('Name', '(unnamed)')))
    terminate_instances(ec2_conn, instances_to_terminate)
    for image in images_to_delete:
        print('Deleting image %s %s %s...' % (image.id, image.name, image.tags.get('Name', '(unnamed)')))
        image.deregister(delete_snapshot=True)
    for group in groups_to_delete:
        print('Deleting security group %s...' % (group.name))
        group.delete()
    print('Ready.')

def benchmark(args):
    tag = args.prefix + '-client'
    
    print('Preparing to run benchmark...')
    print('    Account: %s' % args.key)
    print('    Region: %s' % args.region)
    print('    Instance type: %s' % args.type)
    print('    Tag: %s' % tag)
    
    if args.dry:
        return
    
    ec2_conn = connect_to_region(args.region, aws_access_key_id=args.key, aws_secret_access_key=args.secret)
    auto_choose_ami(ec2_conn, args)
    create_security_group(ec2_conn, args)
    server_address = launch_redis_server(ec2_conn, args)
    redis_client = connect_to_redis_server(server_address)
    reset_redis_data(redis_client)
    ab_command_line = '/usr/bin/ab -n %d -c %d %s %s' % (args.numrequests, args.concurrency, args.options, args.url)
    user_data = make_client_user_data(server_address, ab_command_line)
    
    print('Ready to start benchmarking.')
    print('    URL: %s' % (args.url))
    print('    Instance count: %d' % (args.instances))
    print('    Number of requests: %d' % (args.numrequests))
    print('    Concurrency: %d' % (args.concurrency))
    print('    Additional options: %s' % (args.options))
    print('    Full ab command: %s' % (ab_command_line))
    
    # Now we can launch the instances.
    instances = []
    for n in xrange(0, args.instances):
        reservation = ec2_conn.run_instances(image_id=args.ami, instance_type=args.type, user_data=user_data, key_name=args.keypair, security_groups=[args.group])
        instances.append(reservation.instances[0])
    
    # Wait a moment to let them initialize, and then tag them.
    time.sleep(10)
    for instance in instances:
        instance.add_tag('Name', tag)
        instance.add_tag('StormBench', 'True')
    
    # Wait until all instances have registered on the server.
    wait_for_redis_clients(redis_client, [instance.id for instance in instances])
    
    # Trigger the benchmark everywhere NOW!
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    redis_client.set('trigger', now)
    print('Benchmark triggered at %s!' % now)
    
    # Wait for results from each client.
    results = wait_for_redis_results(redis_client, [instance.id for instance in instances])
    
    # All done.
    print('Benchmark ready.')
    print_results(results)
    
    # Terminate all client instances now.
    terminate_instances(ec2_conn, instances)

def main():
    parser = argparse.ArgumentParser(usage=USAGE)
    parser.add_argument('command', type=str, choices=('startserver', 'stopserver', 'createimage', 'benchmark', 'status', 'cleanup'))
    parser.add_argument('url', type=str, default='', nargs='?')
    parser.add_argument('-d', '--dry', action='store_true')
    parser.add_argument('-r', '--region', type=str, default='eu-west-1')
    parser.add_argument('-a', '--ami', type=str, default='')
    parser.add_argument('-p', '--prefix', type=str, default='stormbench')
    parser.add_argument('-t', '--type', type=str, default='m1.medium')
    parser.add_argument('-k', '--key', type=str, required=True)
    parser.add_argument('-e', '--keypair', type=str, default=None)
    parser.add_argument('-s', '--secret', type=str, required=True)
    parser.add_argument('-g', '--group', type=str, default='stormbench')
    parser.add_argument('-f', '--full', action='store_true')
    parser.add_argument('-i', '--instances', type=int, default=1)
    parser.add_argument('-n', '--numrequests', type=int, default=1)
    parser.add_argument('-c', '--concurrency', type=int, default=1)
    parser.add_argument('-o', '--options', type=str, default='')
    args = parser.parse_args()
    if args.command == 'benchmark' and not args.url:
        print(parser.usage)
        print('URL is required')
        return
    logging.basicConfig(level=logging.CRITICAL)
    if args.dry:
        print('*** DRY RUN ***')
    globals()[args.command](args)

USAGE = re.sub(r'^    ', '', """
    StormBench 1.0 (C) Kenneth Falck <kennu@iki.fi> 2012
    
    Usage: stormbench.py [options] <command> [arguments]
    
    Commands:
    
    stormbench.py [options] createimage
    
        Creates a new AMI client image using a base Ubuntu image. This will
        launch a temporary EC2 instance which is terminated at the end.
    
        -a ami-c7aaabb3
        --ami ami-c7aaabb3    Base AMI image. This will default to the
                              Ubuntu 12.04 EBS 32-bit AMI for the active
                              region. See this URL for a list of images:
                              http://cloud-images.ubuntu.com/releases/precise/release/
        -t m1.large
        --type m1.large       EC2 instance type to use. Default: m1.medium.
    
    stormbench.py [options] benchmark <url>
    
        Runs a benchmark for the specified URL.
    
        <url>                 The URL to test. REQUIRED
        -i 100
        --instances 100       Number of client instances to start. Default: 1
        -n 1000
        --numrequests         Number of requests/client to make. Default: 1
        -c 50
        --concurrency 50      Concurrency of each client. Default: 1
        -o <options>
        --options <options>   Specify additional options for ApacheBench.
                              See man ab(1) for more information.
        -a ami-xxxxxxxx
        --ami ami-xxxxxxxx    AMI image of client instances. The default
                              is automatically detected based on the tag
                              prefix of a previously generated image.
        -t m1.large
        --type m1.large       EC2 instance type to use. Default: m1.medium.
    
    stormbench.py [options] status
    
        Shows the current status and EC2 resources used by StormBench.
    
    stormbench.py [options] cleanup
    
        Cleans up all resources on EC2 that were created by StormBench.
        This command will ask for confirmation before proceeding.
        
        -f
        --full                Full cleanup, also delete latest AMI.
    
    Common options:
    
        -k
        --key                 AWS access key to use. REQUIRED
        -s
        --secret              AWS secret access key to use. REQUIRED
        -r us-east-1
        --region us-east-1    AWS region to use. Default: eu-west-1
        -p tag-prefix
        --prefix tag-prefix   AWS tag prefix. Default: stormbench
        -e key-name
        --keypair key-name    AWS keypair to use. Default: None
        -g sec-group
        --group sec-group     AWS security group. Default: stormbench
        -d
        --dry                 Dry run, don't launch anything on AWS.
    """, flags=re.M)

if __name__ == '__main__':
    main()
