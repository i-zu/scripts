#!/usr/bin/env python3

import argparse
import boto3
import sys
import datetime
import dateutil
import re
import time

PROFILES = ['profile_name']
OWNER = "111111111111"
SERVICES = ['stash', 'confluence', 'jira']
REGIONS = ['us-east-1', 'us-west-1', 'us-west-2', 'eu-west-1', 'eu-central-1', 'ap-southeast-1', 'ap-southeast-2', 'ap-northeast-1', 'sa-east-1']
EIP = {'stash_eip': {'ip_address': ''192.0.2.1, 'allocation_id': 'eipalloc-11111111'},
       'confluence_eip': {'ip_address': '192.0.2.2', 'allocation_id': 'eipalloc-11111111'},
       'jira_eip': {'ip_address': '192.0.2.3', 'allocation_id': 'eipalloc-11111111'}}

VIRGINIA_VPC_ID_STAGING = "vpc-11111111"
VIRGINIA_SUBNET_ID_STAGING="subnet-11111111"
VIRGINIA_SG_ID_STAGING = "sg-11111111"
MIN_INSTANCE_COUNT = 1
MAX_INSTANCE_COUNT = 1
INSTANCE_TYPE = "m3.xlarge"

# Find AMI either in original or destination region
def find_ami(images, image_name=None):
    image_info = list()
    NOW = datetime.datetime.now(datetime.timezone.utc)

    for image in images:
        dt_creation_date = dateutil.parser.parse(image["CreationDate"])
        if image_name:
            if (NOW - dt_creation_date).days < 1 and re.search(image_name, image['Name']):
                image_info.append({"name": image["Name"], "image_id": image["ImageId"], "creation_date": image['CreationDate']}) 
        else:
            if (NOW - dt_creation_date).days < 1 and re.search(service, image['Name']):
                image_info.append({"name": image["Name"], "image_id": image["ImageId"], "creation_date": image['CreationDate']}) 

    if not len(image_info):
        print("No AMIs found")
        sys.exit(2)
    else:
        return image_info

# Getting the name of AMI copy
def get_ami_name(ami_list):
    if len(ami_list) == 1:
        split_name = ami_list[0]["name"].split('-')
        image_copy_name = "{}-{}".format(split_name[1], split_name[2])
        return image_copy_name
    else:
        print("More than one AMI present in the list")
        sys.exit(3)

# Searching for running instances
# As usual only one instance should be running for each Atlassian service
def search_running_instances(ec2_client, service):
    instance_info = list()
    running_instances = ec2_client.describe_instances(Filters=[{"Name": "owner-id", "Values": [OWNER]}])
    for reservation in running_instances['Reservations']:
        for tag in reservation['Instances'][0]['Tags']:
            if tag['Key'] == 'Name' and re.search(service, tag['Value']) and re.match('running', reservation['Instances'][0]['State']['Name']):
                instance_info.append({'instance_id': reservation['Instances'][0]['InstanceId'],
                                    'instance_name': tag['Value'],
                                    'block_device_mappings': reservation['Instances'][0]['BlockDeviceMappings']})

    if len(instance_info) > 1:
        print("More than one instance is running")
        sys.exit(4)
    elif not len(instance_info):
        print("No running instances found")

    return instance_info

# Service should be passed as positional argument
parser = argparse.ArgumentParser()
parser.add_argument("service", help="Name of service you want to launch", choices = SERVICES)
args = parser.parse_args()
service = args.service

# Inititating sessions and EC2 clients
session_oregon = boto3.Session(profile_name = PROFILES[0], region_name='us-west-2')
session_virginia = boto3.Session(profile_name = PROFILES[0], region_name='us-east-1')

ec2_client_oregon = session_oregon.client('ec2')
ec2_client_virginia = session_virginia.client('ec2')

# Get list of AMIs which belong to our account only
response = ec2_client_oregon.describe_images(Filters=[{"Name": "owner-id", "Values": [OWNER]}])
images_oregon = response['Images']

response = ec2_client_virginia.describe_images(Filters=[{"Name": "owner-id", "Values": [OWNER]}])
images_virginia = response['Images']


# Searching for running instance to terminate it before launching the fresh one
running_instance = search_running_instances(ec2_client_virginia, service)
if len(running_instance):
    running_instance_id = running_instance[0]['instance_id']
    running_instance_name = running_instance[0]['instance_name']
    running_instance_ebs = list()
    for block_device_mapping in running_instance[0]['block_device_mappings']:
        running_instance_ebs.append({'volume_id': block_device_mapping['Ebs']['VolumeId'],
                                    'delete_on_termination': block_device_mapping['Ebs']['DeleteOnTermination']})

    print("Removing instance '{}' with id '{}'".format(running_instance_name, running_instance_id))
    ec2_client_virginia.terminate_instances(InstanceIds=[running_instance_id])
    for ebs in running_instance_ebs:
        if not ebs['delete_on_termination']:
            ebs_response = ec2_client_virginia.describe_volumes(Filters=[{'Name': 'volume-id', 'Values':[ebs['volume_id']]}])
            ebs_state = ebs_response['Volumes'][0]['State']
            while not ebs_state == 'available':
                time.sleep(10)
                ebs_response = ec2_client_virginia.describe_volumes(Filters=[{'Name': 'volume-id', 'Values':[ebs['volume_id']]}])
                ebs_state = ebs_response['Volumes'][0]['State']
            print("Removing EBS volume with id '{}'".format(ebs['volume_id']))
            ec2_client_virginia.delete_volume(VolumeId=ebs['volume_id'])

# Finding fresh AMIs and launching new instance
ami_oregon = find_ami(images_oregon)
image_copy_name = get_ami_name(ami_oregon)
ami_virginia = find_ami(images_virginia, image_copy_name)

launch_ami_id = ami_virginia[0]['image_id']

# Domain name for our Confluence is 'docs'
if service == 'confluence':
    name = 'docs'
else:
     name = service
node_name = "aws{}.example.com".format(name)
role_name = "Atlassian-{}-Staging".format(service.title())
hostname = "stg{}.example.com".format(name)
user_data = """#!/bin/bash
hostname {}
echo {} > /etc/hostname
rm -f /etc/chef/client.rb /etc/chef/client.pem
chef-client -S "https://chef.example.com" -N "{}" -E "CHEF_ENV" -r "role[{}]"
""".format(hostname, hostname, node_name, role_name)

# Launching new instance using known AMI
print("Launching instance from AMI {}".format(launch_ami_id))
new_instance = ec2_client_virginia.run_instances(ImageId=launch_ami_id,
                                          MinCount=MIN_INSTANCE_COUNT,
                                          MaxCount=MAX_INSTANCE_COUNT,
                                          InstanceType=INSTANCE_TYPE,
                                          NetworkInterfaces=[
                                              {'DeviceIndex': 0, 
                                               'AssociatePublicIpAddress': True,
                                               'SubnetId': VIRGINIA_SUBNET_ID_STAGING,
                                               'Groups': [VIRGINIA_SG_ID_STAGING]}],
                                          UserData=user_data)
new_instance_id = new_instance['Instances'][0]['InstanceId']
new_instance_name = "{} staging".format(service)
# Adding tags after image has been created
create_tag_response = ec2_client_virginia.create_tags(Resources=[new_instance_id],
                                               Tags=[{'Key': 'Name', 'Value': new_instance_name}])
# Associate EIP with created instance
new_instance_response=ec2_client_virginia.describe_instances(Filters=[{'Name': 'instance-id', 'Values': [new_instance_id]}])
new_instance_state = new_instance_response['Reservations'][0]['Instances'][0]['State']['Name']
while not new_instance_state == 'running':
    time.sleep(10)
    new_instance_response=ec2_client_virginia.describe_instances(Filters=[{'Name': 'instance-id', 'Values': [new_instance_id]}])
    new_instance_state = new_instance_response['Reservations'][0]['Instances'][0]['State']['Name']
associate_eip_response = ec2_client_virginia.associate_address(InstanceId=new_instance_id,
                                                      PublicIp=EIP[service+'_eip']['ip_address'],
                                                      AllowReassociation=False)
