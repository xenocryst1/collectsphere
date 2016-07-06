"""
This is the code that needs to be integrated into collectd when run in
production. It contains the python code that integrates into the python module
for collectd. It connects to one or more vCenter Servers and gathers the configured
metrics from ESXi hosts and Virtual Machines.

The file is organized in multiple sections. The first section implements the
callback functions executed be collectd which is followed be a couple of helper
functions that separate out some code to make the rest more readable. The
helper classes section provides threads that are used to parallelize things and
make the plugin a lot faster.
"""
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4

import collectd
import logging
import threading
import time
import ssl
import re
import datetime

from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
from time import localtime

################################################################################
# CONFIGURE ME
################################################################################
INTERVAL = 60

################################################################################
# DO NOT CHANGE BEYOND THIS POINT!
################################################################################
CONFIGS = []  # Stores the configuration as passed from collectd
ENVIRONMENT = {}  # Runtime data and object cache

################################################################################
# IMPLEMENTATION OF COLLECTD CALLBACK FUNCTIONS
################################################################################

def configure_callback(conf):
    """Receive configuration block. This is called by collectd for every
    configuration block it finds for this module."""

    # Set some sensible default values
    name = None
    host = None
    port = 443
    verbose = None
    username = 'root'
    password = 'vmware'
    host_counters = []
    vm_counters = []
    inventory_refresh_interval = 600

    for node in conf.children:
        key = node.key.lower()
        val = node.values

        if key == 'name':
            name = val[0]
        elif key == 'host':
            host = val[0]
        elif key == 'port':
            port = int(val[0])
        elif key == 'verbose':
            verbose = bool(val)
        elif key == 'username':
            username = val[0]
        elif key == 'password':
            password = val[0]
        elif key == 'host_counters':
            str = val[0]
            if not str == "all":
                values = str.split(',')
                for m in values:
                    if len(m) > 0:
                        host_counters.append(m.strip())
        elif key == 'vm_counters':
            str = val[0]
            if not str == "all":
                values = str.split(',')
                for m in values:
                    if len(m) > 0:
                        vm_counters.append(m.strip())
        elif key == 'inventory_refresh_interval':
            inventory_refresh_interval = int(val[0])
        else:
            collectd.warning('collectsphere plugin: Unknown config key: %s.' 
                             % key)
            continue

    collectd.info('configure_callback: Loaded config: name=%s, host=%s, port=%s, verbose=%s, username=%s, password=%s, host_metrics=%s, vm_metrics=%s, inventory_refresh_interval=%s' % (name, host, port, verbose, username, "******", len(host_counters), len(vm_counters), inventory_refresh_interval))

    CONFIGS.append({
        'name': name,
        'host': host,
        'port': port,
        'verbose': verbose,
        'username': username,
        'password': password,
        'host_counters': host_counters,
        'vm_counters': vm_counters,
        'inventory_refresh_interval': inventory_refresh_interval
    })

def init_callback():
    """ In this method we create environments for every configured vCenter
    Server. This includes creating the connection, reading in counter ID
    mapping tables """

    # For every set of configuration received from collectd, a environment must
    # be created.
    for config in CONFIGS:
        env = create_environment(config)

        # The environment is stored under the name of the config block
        ENVIRONMENT[config.get("name")] = env

def read_callback():
    """ This function is regularly executed by collectd. It is important to
    minimize the execution time of the function which is why a lot of caching
    is performed using the environment objects. """

    # Walk through the existing environments
    for name in ENVIRONMENT.keys():
        env = ENVIRONMENT[name]
        collectd.info("read_callback: entering environment: " + name)

        # Connects to vCenter Server
        serviceInstance = SmartConnect(host = env["host"], user = env["username"], pwd = env["password"])
        performanceManager = serviceInstance.RetrieveServiceContent().perfManager

        # Walk through all Clusters of Datacenter
        for datacenter in serviceInstance.RetrieveServiceContent().rootFolder.childEntity:
            if datacenter._wsdlName == "Datacenter":
                for cluster in datacenter.hostFolder.childEntity:
                    if cluster._wsdlName == "ClusterComputeResource":

                        # Walk throug all hosts in cluster, collect its metrics and dispatch them
                        collectd.info("read_callback: found %d hosts in cluster %s" % (len(cluster.host), cluster.name))
                        colletMetricsForEntities(performanceManager, env['host_counter_ids'], cluster.host, cluster.name)

                        # Walk throug all vms in host, collect its metrics and dispatch them
                        for host in cluster.host:
                            if host._wsdlName == "HostSystem":
                                collectd.info("read_callback: found %d vms in host %s" % (len(host.vm), host.name))
                                colletMetricsForEntities(performanceManager, env['vm_counter_ids'], host.vm, cluster.name)

def colletMetricsForEntities(performanceManager, filteredMetricIds, entities, cluster_name):

    # Definition of the queries for getting performance data from vCenter
    qSpecs = []
    qSpec = vim.PerformanceManager.QuerySpec()
    qSpec.metricId = filteredMetricIds
    qSpec.format = "normal"
    # Define the default time range in which the data should be collected (from
    # now to INTERVAL seconds)
    endTime = datetime.datetime.today()
    startTime = datetime.datetime.today() - datetime.timedelta(seconds = INTERVAL)
    qSpec.endTime = endTime
    qSpec.startTime = startTime
    # Define the interval, in seconds, for the performance statistics. This
    # means for any entity and any metric there will be
    # INTERVAL / qSpec.intervalId values collected. Leave blank or use
    # performanceManager.historicalInterval[i].samplingPeriod for
    # aggregated values
    qSpec.intervalId = 20

    # For any entity there has to be an own query.
    if len(entities) == 0:
        return
    for entity in entities:
        qSpec.entity = entity
        qSpecs.append(qSpec)

    # Retrieves the performance metrics for the specified entity (or entities)
    # based on the properties specified in the qSpecs
    collectd.info("GetMetricsForEntities: collecting its stats")
    metricsOfEntities = performanceManager.QueryPerf(qSpecs)

    cd_value = collectd.Values(plugin = "collectsphere")
    cd_value.type = "gauge"

    # Walk throug all entites of query
    for p in range(len(metricsOfEntities)):
        metricsOfEntity = metricsOfEntities[p].value

        # For every queried metric per entity, get an array consisting of
        # performance counter information for the specified counterIds.
        queriedCounterIdsPerEntity = []
        for metric in metricsOfEntity :
            queriedCounterIdsPerEntity.append(metric.id.counterId)
        perfCounterInfoList = performanceManager.QueryPerfCounter(queriedCounterIdsPerEntity)

        # Walk thorug all queried metrics per entity
        for o in range(len(metricsOfEntity)) :
            metric = metricsOfEntity[o]

            perfCounterInfo = perfCounterInfoList[o]
            counter = perfCounterInfo.nameInfo.key
            group = perfCounterInfo.groupInfo.key
            instance = metric.id.instance
            unit = perfCounterInfo.unitInfo.key
            rollupType = perfCounterInfo.rollupType

            # Walk throug all values of a metric (INTERVAL / qSpec.intervalId values)
            for i in range(len(metric.value)) :
                value = float(metric.value[i])

                # Get the timestamp of value. Because of an issue by VMware the
                # has to be add an hour if you're at DST
                timestamp = float(time.mktime(metricsOfEntities[p].sampleInfo[i].timestamp.timetuple()))
                timestamp += time.localtime().tm_isdst * (3600)
                cd_value.time = timestamp

                # truncate
                instance = truncate(instance)
                # When the instance value is empty, the vSphere API references a
                # total. Example: A host has multiple cores for each of which we
                # get a single stat object. An additional stat object will be
                # returned by the vSphere API with an empty string for "instance".
                # This is the overall value accross all logical CPUs.
                # if(len(stat.instance.strip()) == 0):
                #   instance = 'all'
                instance = "all" if instance == "" else instance
                unit = truncate(unit)
                group = truncate(group)
                if rollupType == vim.PerformanceManager.CounterInfo.RollupType.maximum :
                    print ""
                rollupType = truncate(rollupType)
		entitiesName = FQDNtruncate(entities[p].name)
 		ObjectType = Objtruncate(entities[p]._wsdlName)
                type_instance_str = cluster_name + "." + ObjectType + "." + entitiesName + "." + group + "." + instance + "." + rollupType + "." + counter + "." + unit
                type_instance_str = type_instance_str.replace(' ', '_')

                # now dispatch to collectd
                cd_value.dispatch(time = timestamp, type_instance = type_instance_str, values = [value])

def shutdown_callback():
    """ Called by collectd on shutdown. """
    None


################################################################################
# HELPER FUNCTIONS
################################################################################

def truncate(str):
    """ We are limited to 63 characters for the type_instance field. This
    function truncates names in a sensible way """

    # NAA/T10 Canonical Names
    m = re.match('(naa|t10)\.(.+)', str, re.IGNORECASE)
    if m:
        id_type = m.group(1).lower()
        identifier = m.group(2).lower()
        if identifier.startswith('ATA'):
            m2 = re.match('ATA_+(.+?)_+(.+?)_+', identifier, re.IGNORECASE)
            identifier = m2.group(1) + m2.group(2)
        else:
            str = id_type + identifier[-12:]

    # vCloud Director naming pattern
    m = re.match('^(.*)\s\(([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\)(.*)$', str, re.IGNORECASE)
    if m:
        vm_name = m.group(1).lower()
        uuid = m.group(2).lower()
        suffix = m.group(3).lower()
        short_vm_name = vm_name[:6]
        short_uuid = uuid[:6]
        str = short_vm_name + '-' + short_uuid + suffix

    # VMFS UUIDs: e.g. 541822a1-d2dcad52-129a-0025909ac654
    m = re.match('^(.*)([0-9a-f]{8}-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{12})(.*)$', str, re.IGNORECASE)
    if m:
        before = m.group(1).lower()
        uuid = m.group(2).lower()
        after = m.group(3).lower()
        short_uuid = uuid[:12]
        str = before + short_uuid + after

    # truncate units
    str = str.replace('millisecond', 'ms')
    str = str.replace('percent', 'perc')
    str = str.replace('number', 'num')
    str = str.replace('kiloBytesPerSecond', 'KBps')
    str = str.replace('kiloBytes', 'KB')
    str = str.replace('megaBytes', 'MB')

    # truncate groups
    str = str.replace('datastore', 'ds')

    return str

def FQDNtruncate(str):

    # truncate fqdn
    m = re.match('(?=^.{1,254}$)(^(?:(?!\d+\.|-)[a-zA-Z0-9_\-]{1,63}(?<!-)\.?)+(?:[a-zA-Z]{2,})$)', str, re.IGNORECASE)
    if m:
        str = str.split(".", 1)[0]

    return str

def Objtruncate(str):

    # truncate object type
    if str == 'HostSystem':
	str = str.replace('HostSystem', 'HS')
    elif str == 'VirtualMachine':
	str = str.replace('VirtualMachine', 'VM')

    return str

def create_environment(config):
    """
    Creates a runtime environment from a given configuration block. As the
    structure of an environment is a bit complicates, this is the time to
    document it:

    A single environment is a dictionary that stores runtime information about
    the connection, metrics, etc for a single vCenter Server. This is the
    structure pattern:

        {
            'host': <FQDN OR IP ADDRESS OF VCENTER SERVER>,
            'username': <USER FOR LOGIN IN VCENTER SERVER>,
            'password': <PASSWORD FOR LOGIN IN VCENTER SERVER>,

            # This is a dictionary that stores mappings of performance counter
            # names to their respective IDs in vCenter.
            'lookup_host': {
                'NAME': <ID>,       # Example: 'cpu.usage': 2
                ...
            },

            # The same lookup dictionary must be available for virtual machines:
            'lookup_vm': {
                'NAME': <ID>,
                ...
            },

            # This stores the IDs of the counter names passed via the
            # configuration block. We used the lookup tables above to fill in
            # the IDs.
            'host_counter_ids': [<ID>, <ID>, ...],
            'vm_counter_ids': [<ID>, <ID>, ...],
        }
    """

    # Connect to vCenter Server
    serviceInstance = SmartConnect(host = config.get("host"), user = config.get("username"), pwd = config.get("password"))

    # If we could not connect abort here
    if not serviceInstance:
        print("Could not connect to the specified host using specified "
             "username and password")
        return -1

    # Set up the environment. We fill in the rest afterwards.
    env = {}
    env["host"] = config.get("host")
    env["username"] = config.get("username")
    env["password"] = config.get("password")

    performanceManager = serviceInstance.RetrieveServiceContent().perfManager

    # We need at least one host and one virtual machine, which are poweredOn, in
    # the vCenter to be able to fetch the Counter IDs and establish the lookup table.
    
    # Fetch the Counter IDs
    filteredCounterIds = []
    for perfCounter in performanceManager.perfCounter:
        counterKey = perfCounter.groupInfo.key + "." + perfCounter.nameInfo.key;
        if counterKey in config['vm_counters'] + config['host_counters'] :
            filteredCounterIds.append(perfCounter.key)
    
    host = None
    vm = None
    for child in serviceInstance.RetrieveServiceContent().rootFolder.childEntity:
        if child._wsdlName == "Datacenter":
            for hostFolderChild in child.hostFolder.childEntity:
                host = hostFolderChild.host[0] if ((len(hostFolderChild.host) != 0) and hostFolderChild.host[0].summary.runtime.powerState == vim.HostSystem.PowerState.poweredOn) else host
                if (vm != None and host != None):
                    break
            vmList = child.vmFolder.childEntity
            for tmp in vmList:
                if tmp._wsdlName == "VirtualMachine":
                    if tmp.summary.runtime.powerState == vim.VirtualMachine.PowerState.poweredOn:
                        vm = tmp
                        if vm != None and host != None:
                            break
                elif tmp._wsdlName == "Folder":
                    vmList += tmp.childEntity
                elif tmp._wsdlName == "VirtualApp":
                    vmList += tmp.vm
    if(host == None):
        collectd.info("create_environment: vCenter " + config.get("name") + " does not contain any hosts. Cannot continue")
        return
    if(vm == None):
        collectd.info("create_environment: vCenter " + config.get("name") + " does not contain any VMs. Cannot continue")
        return

    # Get all queryable aggregated and realtime metrics for an entity
    env['lookup_host'] = []
    env['lookup_vm'] = []
    perfI = performanceManager.historicalInterval[0];
    perfI.level = 2;

    # Update performance interval to get all rolluptypes
    performanceManager.UpdatePerfInterval(perfI);

    # Query aggregated qureyable mertics for host and vm
    env['lookup_host'] += performanceManager.QueryAvailablePerfMetric(host, None, None, perfI.samplingPeriod)
    env['lookup_vm'] += performanceManager.QueryAvailablePerfMetric(vm, None, None, perfI.samplingPeriod)
    # Query aggregated realtime mertics for host and vm
    env['lookup_host'] += performanceManager.QueryAvailablePerfMetric(host, None, None, 20)
    env['lookup_vm'] += performanceManager.QueryAvailablePerfMetric(vm, None, None, 20)

    # Now use the lookup tables to find out the IDs of the counter names given
    # via the configuration and store them as an array in the environment.
    # If host_counters or vm_counters is empty, select all.
    env['host_counter_ids'] = []
    if len(config['host_counters']) == 0:
        collectd.info("create_environment: configured to grab all host counters")
        env['host_counter_ids'] = env['lookup_host']
    else:
        for metric in env['lookup_host']:
            if metric.counterId in filteredCounterIds:
                env['host_counter_ids'].append(metric)

    collectd.info("create_environment: configured to grab %d host counters" % (len(env['host_counter_ids'])))

    env['vm_counter_ids'] = []
    if len(config['vm_counters']) == 0:
        env['vm_counter_ids'] = env['lookup_vm']
    else:
        for metric in env['lookup_vm']:
            if metric.counterId in filteredCounterIds:
                env['vm_counter_ids'].append(metric)

    collectd.info("create_environment: configured to grab %d vm counters" % (len(env['vm_counter_ids'])))

    return env

################################################################################
# COLLECTD REGISTRATION
################################################################################

collectd.register_config(configure_callback)
collectd.register_init(init_callback)
collectd.register_read(callback = read_callback, interval = INTERVAL)
collectd.register_shutdown(shutdown_callback)
