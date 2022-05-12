import sys
import boto3
import logging
import botocore.exceptions
import time
from datetime import datetime,timedelta

logging.basicConfig(filename='vpc_slayer.log',
                    encoding='utf-8', level=logging.DEBUG)

logger = logging.getLogger()

def get_regions():
    region_list = []
    ec2 = boto3.client('ec2')
    all_regions = ec2.describe_regions()
    for r in all_regions['Regions']:
        region_list.append(r['RegionName'])
    return region_list

def slay_eips(region):
    client = boto3.client('ec2', region_name=region)
    addresses_dict = client.describe_addresses()
    slayed = 0
    failed = 0

    for eip_dict in addresses_dict['Addresses']:
        if "AssociationId" not in eip_dict:
            try:
                slayed += 1
                client.release_address(AllocationId=eip_dict['AllocationId'])
            except Exception as e:
                failed =+ 1
                print(e)
    return slayed, failed

def ebs_slayer(region, age, dryrun):
    ec2 = boto3.resource('ec2', region_name=region)
    ec2client = ec2.meta.client
    threshold = (datetime.now() - timedelta(days=age)).timestamp()
    unattached = []
    slayable_vols = []
    volumes = ec2.volumes.all()
    for volume in volumes:
        if len(volume.attachments) == 0:
            unattached.append(volume)
    
    for volume in unattached:
        if volume.create_time.timestamp() < threshold:
            slayable_vols.append(volume)
    
    failed = 0
    for volume in slayable_vols:
        try: 
            volume.delete(DryRun=dryrun)
        except Exception as e:
            failed = + 1
            print(e)
    
    return len(slayable_vols), failed
    
def get_vpcs(region):
    ec2 = boto3.resource('ec2', region_name=region)
    ec2client = ec2.meta.client
    all_vpcs = ec2client.describe_vpcs(Filters=[{'Name': 'isDefault', 'Values': ['false']}])
    vpc_list = []
    for index in range(len(all_vpcs['Vpcs'])):
        vpc_list.append(all_vpcs['Vpcs'][index]['VpcId'])
    return vpc_list

def ec2_in_vpc(vpcid, region):
    '''checks if there are any EC2 Instances in a VPC'''
    ec2 = boto3.resource('ec2', region)
    ec2client = ec2.meta.client
    vpc = ec2.Vpc(vpcid)

    instances = vpc.instances.all()
    ec2_list =[] 
    for instance in instances:
        ec2_list.append(instance.id)
    
    if not ec2_list:
        return False
    else:
        return True

def rds_slayer():
    '''check on the status of RDS / kill instances'''
    pass

def ec2_slayer(vpcid, region, age, DryRun):
    # Cleaning up very old instances
    ec2 = boto3.resource('ec2', region)
    ec2client = ec2.meta.client
    vpc = ec2.Vpc(vpcid)
    
    old_ec2 = []
    ec2_slayed = 0
    attempted_slays = 0
    instances = vpc.instances.all()
    threshold = (datetime.now() - timedelta(days=age)).timestamp()
    stoped_running = []
    
    for instance in instances:
        if instance.state['Name'] in ['running', 'stopped']:
            stoped_running.append(instance.id)
            if instance.launch_time.timestamp() < threshold:
                old_ec2.append(instance)
    
    for instance in old_ec2:
        try:
            attempted_slays += 1
            instance.terminate(DryRun=DryRun)
            ec2_slayed += 1
        except Exception as e:
            print(e)
    return ec2_slayed, attempted_slays

def elb_slayer(region, age):
    elb = boto3.client('elb', region)
    threshold = (datetime.now() - timedelta(days=age))
    tries = 0
    fails = 0
    allElbs = elb.describe_load_balancers()

    for lb in allElbs['LoadBalancerDescriptions']:
        if len(lb['Instances']) == 0:
            try:
                tries += 1
                elb.delete_load_balancer(
                    LoadBalancerName=lb['LoadBalancerName'])
            except Exception as e:
                fails += 1
                print(e)
    
    #look for old ELBs
    for lb in allElbs['LoadBalancerDescriptions']:
        createtime = lb['CreatedTime']
        createtime = createtime.replace(tzinfo=None)
        if createtime < threshold:
            try:
                tries += 1
                elb.delete_load_balancer(
                    LoadBalancerName=lb['LoadBalancerName'])
            except Exception as e:
                fails += 1
                print(e)

    return tries, fails

# obliterates a vpc
def vpc_cleanup(vpcid, region):
    vpc_slayed = 0
    ec2 = boto3.resource('ec2', region)
    ec2client = ec2.meta.client
    vpc = ec2.Vpc(vpcid)
    logger.info(f'Attempting removal VPC {vpcid} from AWS {region}')
   
### Search for and remove attached Nat Gateways
    for ep in ec2client.describe_nat_gateways(
        Filters=[
            {
            'Name': 'vpc-id',
            'Values': [vpcid]
            }
        ]
        )['NatGateways']:
            try:
                ec2client.delete_nat_gateway(
                NatGatewayId=ep['NatGatewayId'])
            except Exception as e:
                print(e)
                continue

### Search for and remove attached internet Gateways
    for ep in ec2client.describe_internet_gateways(
            Filters=[
                {'Name': 'attachment.vpc-id',
                 'Values': [vpcid]
                 }
            ]
            )['InternetGateways']:
                try:
                    vpc.detach_internet_gateway(InternetGatewayId=ep['InternetGatewayId'])
                    ec2client.delete_internet_gateway(
                        InternetGatewayId=ep['InternetGatewayId'])
                except botocore.exceptions.ClientError as error:
                    if error.response['Error']['Code'] == 'DependencyViolation':
                        logger.warning("waiting for network interfaces to be deleted")
                    continue
    
     # remove interfaces and subnets
    for subnet in vpc.subnets.all():
        for interface in subnet.network_interfaces.all():
            try:
                interface.delete()
            except:
                continue
    for subnet in vpc.subnets.all():
        try:    
            subnet.delete()
        except:
                continue

    # delete all route table associations
    for rt in vpc.route_tables.all():
        for rta in rt.associations:
            if not rta.main:
                try:
                    rta.delete()
                except Exception as e:
                    print(e)

    # remove route tables
    for rt in vpc.route_tables.all():
        try:
            rt.delete()
        except:
            continue

    # delete our endpoints
    for ep in ec2client.describe_vpc_endpoints(
            Filters=[{
                'Name': 'vpc-id',
                'Values': [vpcid]
            }])['VpcEndpoints']:
        try:
            ec2client.delete_vpc_endpoints(VpcEndpointIds=[ep['VpcEndpointId']])
        except Exception as e:
            print(e)
   
    # delete our security groups
    for sg in vpc.security_groups.all():
        if sg.group_name != 'default':
            try:
                sg.delete()
            except Exception as e:
                print(e)
   
    # delete non-default network acls
    for netacl in vpc.network_acls.all():
        if not netacl.is_default:
            try:
                netacl.delete()
            except Exception as e:
                print(e)

    # delete any vpc peering connections
    for vpcpeer in ec2client.describe_vpc_peering_connections(
            Filters=[{
                'Name': 'requester-vpc-info.vpc-id',
                'Values': [vpcid]
            }])['VpcPeeringConnections']:
            try:
                ec2.VpcPeeringConnection(vpcpeer['VpcPeeringConnectionId']).delete()
            except Exception as e:
                print(e)

    for vpcpeer in ec2client.describe_vpc_peering_connections(
            Filters=[{
                'Name': 'accepter-vpc-info.vpc-id',
                'Values': [vpcid]
            }])['VpcPeeringConnections']:
            try:
                ec2.VpcPeeringConnection(
                    vpcpeer['VpcPeeringConnectionId']).delete()
            except Exception as e:
                print(e)
   
    # finally, delete the vpc
    try:
        ec2client.delete_vpc(VpcId=vpcid)
        vpc_slayed = True
    except Exception as e:
        print(e)

    return vpc_slayed

def main(argv=None):
    #regions = ["us-east-2"]
    age = 60
    dryrun = False
    
    regions = get_regions()

    #Slay out of serivce ELBs
    for region in regions:
        tries, fails = elb_slayer(region, age)
        print(f'{region}:ELB: Attempted {tries} kills. Failed on {fails}')

    # Slay unatached EIPs
    for region in regions:
        tries, failed = slay_eips(region)
        print(
            f'{region}:EIP: Attempted {tries} kills. Failed on {failed}')

    # Slay Old, Unattached EBS Volumes
    for region in regions:
        canidates, failed = ebs_slayer(region, age, dryrun)
        print(f'{region}:EBS: Tried to slay {canidates}, failed on {failed}')
        logger.info(f'{region}:EBS: Tried to slay {canidates}, failed on {failed}')
    
    # Slay Old EC2s
    for region in regions:
        slayed = 0
        attempted_slays = 0
        vpc_ids = get_vpcs(region)

        for vpc in vpc_ids:
            kills, attempts = ec2_slayer(vpc, region, age, dryrun)
            slayed += kills
            attempted_slays += attempts

        print(
            f'{region}:EC2: Attempted {attempted_slays} kills. Success on {slayed}')

    # Sit tight while the ec2 instances spin down
    time.sleep(60)

    # Slay old RDS Clusters and Instances

    # Slay old RDS Snapshots

    # Slay old ECS

    # Clean Up Empty VPCs
    for region in regions:
        '''get total number of VPCs in Region'''
        vpc_ids = get_vpcs(region)
        print(f'{region}: There are {len(vpc_ids)} vpcs')

        '''Gather List of Empty VPCs'''
        empty_vpcs = []
        vpc_headcount = 0

        for vpc in vpc_ids:
            status = None
            status = ec2_in_vpc(vpc,region)
           
           # negative result means the vpc has no ec2
            if not status:
                empty_vpcs.append(vpc)

        print(f'{region}: Found {len(empty_vpcs)} VPCs with no EC2')
        
        if not dryrun:
            for vpc in empty_vpcs:
                print(f"killing {vpc}")  
                slayed_vpcs = []
                result = vpc_cleanup(vpc, region)
                if result:
                    vpc_headcount += 1

        print(f'{region}: Removed {vpc_headcount} VPCs')

if __name__ == '__main__':
    main(sys.argv)
