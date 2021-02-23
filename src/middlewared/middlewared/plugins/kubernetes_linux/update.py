import ipaddress
import itertools
import os

import middlewared.sqlalchemy as sa

from middlewared.common.listen import ConfigServiceListenSingleDelegate
from middlewared.schema import Dict, IPAddr, Str
from middlewared.service import accepts, CallError, job, private, ConfigService, ValidationErrors


class KubernetesModel(sa.Model):
    __tablename__ = 'services_kubernetes'

    id = sa.Column(sa.Integer(), primary_key=True)
    pool = sa.Column(sa.String(255), default=None, nullable=True)
    cluster_cidr = sa.Column(sa.String(128), default='')
    service_cidr = sa.Column(sa.String(128), default='')
    cluster_dns_ip = sa.Column(sa.String(128), default='')
    route_v4_interface = sa.Column(sa.String(128), nullable=True)
    route_v4_gateway = sa.Column(sa.String(128), nullable=True)
    route_v6_interface = sa.Column(sa.String(128), nullable=True)
    route_v6_gateway = sa.Column(sa.String(128), nullable=True)
    node_ip = sa.Column(sa.String(128), default='0.0.0.0')
    cni_config = sa.Column(sa.JSON(type=dict))


class KubernetesService(ConfigService):

    class Config:
        datastore = 'services.kubernetes'
        datastore_extend = 'kubernetes.k8s_extend'
        cli_namespace = 'app.kubernetes'

    @private
    async def k8s_extend(self, data):
        data['dataset'] = os.path.join(data['pool'], 'ix-applications') if data['pool'] else None
        data.pop('cni_config')
        return data

    @private
    async def unused_cidrs(self, network_cidrs):
        return [
            str(network) for network in itertools.chain(
                ipaddress.ip_network('172.16.0.0/12', False).subnets(4),
                ipaddress.ip_network('10.0.0.0/8', False).subnets(8)
            ) if not any(network.overlaps(used_network) for used_network in network_cidrs)
        ]

    @private
    async def validate_data(self, data, schema):
        verrors = ValidationErrors()

        network_cidrs = [
            ipaddress.ip_network(f'{ip_config["address"]}/{ip_config["netmask"]}', False)
            for interface in await self.middleware.call('interface.query')
            for ip_config in interface['aliases']
        ]

        unused_cidrs = []
        if not data['cluster_cidr'] or not data['service_cidr']:
            unused_cidrs = await self.unused_cidrs(network_cidrs)
            # If index 0,1 belong to different classes, let's make sure
            # that is not the case anymore - we will have class b first and then class a, so if
            # index 0 is class b and 1 is class a, we remove index 0
            class_b = ipaddress.IPv4Network(('172.16.0.0', '255.240.0.0'))
            if len(unused_cidrs) > 2 and class_b.overlaps(
                ipaddress.ip_network(unused_cidrs[0], False)
            ) and not class_b.overlaps(ipaddress.ip_network(unused_cidrs[1], False)):
                unused_cidrs.pop(0)

        if unused_cidrs and not data['cluster_cidr']:
            data['cluster_cidr'] = unused_cidrs.pop(0)

        if unused_cidrs and not data['service_cidr']:
            data['service_cidr'] = unused_cidrs.pop(0)

        if not data['cluster_dns_ip'] and data['service_cidr']:
            # Picking 10th ip ( which is the usual default ) from service cidr
            data['cluster_dns_ip'] = str(list(ipaddress.ip_network(data['service_cidr'], False).hosts())[9])

        if data['pool'] and not await self.middleware.call('pool.query', [['name', '=', data['pool']]]):
            verrors.add(f'{schema}.pool', 'Please provide a valid pool configured in the system.')

        for k in ('cluster_cidr', 'service_cidr'):
            if not data[k]:
                verrors.add(f'{schema}.{k}', f'Please specify a {k.split("_")[0]} CIDR.')
            elif any(ipaddress.ip_network(data[k], False).overlaps(cidr) for cidr in network_cidrs):
                verrors.add(f'{schema}.{k}', 'Requested CIDR is already in use.')

        if data['cluster_cidr'] and data['cluster_cidr'] == data['service_cidr']:
            verrors.add(f'{schema}.cluster_cidr', 'Value cannot be similar to service CIDR.')
            verrors.add(f'{schema}.service_cidr', 'Value cannot be similar to cluster CIDR.')

        if data['cluster_cidr'] and ipaddress.ip_network(data['cluster_cidr'], False).overlaps(
            ipaddress.ip_network(data['service_cidr'], False)
        ):
            verrors.add(f'{schema}.cluster_cidr', 'Must not overlap with service CIDR.')

        if data['service_cidr'] and ipaddress.ip_address(
            data['cluster_dns_ip']
        ) not in ipaddress.ip_network(data['service_cidr']):
            verrors.add(f'{schema}.cluster_dns_ip', 'Must be in range of "service_cidr".')

        if data['node_ip'] not in await self.bindip_choices():
            verrors.add(f'{schema}.node_ip', 'Please provide a valid IP address.')

        for k, _ in await self.validate_interfaces(data):
            verrors.add(f'{schema}.{k}', 'Please specify a valid interface.')

        for k in ('route_v4', 'route_v6'):
            gateway = data[f'{k}_gateway']
            interface = data[f'{k}_interface']
            if (not gateway and not interface) or (gateway and interface):
                continue
            for k2 in ('gateway', 'interface'):
                verrors.add(f'{schema}.{k}_{k2}', f'{k}_gateway and {k}_interface must be specified together.')

        verrors.check()

    @private
    async def validate_interfaces(self, data):
        errors = []
        interfaces = {i['name']: i for i in await self.middleware.call('interface.query')}
        for k in filter(
            lambda k: data[k] and data[k] not in interfaces, ('route_v4_interface', 'route_v6_interface')
        ):
            errors.append((k, data[k]))
        return errors

    @accepts(
        Dict(
            'kubernetes_update',
            Str('pool', empty=False, null=True),
            IPAddr('cluster_cidr', cidr=True, empty=True),
            IPAddr('service_cidr', cidr=True, empty=True),
            IPAddr('cluster_dns_ip', empty=True),
            IPAddr('node_ip'),
            Str('route_v4_interface', null=True),
            IPAddr('route_v4_gateway', null=True, v6=False),
            Str('route_v6_interface', null=True),
            IPAddr('route_v6_gateway', null=True, v4=False),
            update=True,
        )
    )
    @job(lock='kubernetes_update')
    async def do_update(self, job, data):
        """
        `pool` must be a valid ZFS pool configured in the system. Kubernetes service will initialise the pool by
        creating datasets under `pool_name/ix-applications`.

        `cluster_cidr` is the CIDR to be used for default NAT network between workloads.

        `service_cidr` is the CIDR to be used for kubernetes services which are an abstraction and refer to a
        logically set of kubernetes pods.

        `cluster_dns_ip` is the IP of the DNS server running for the kubernetes cluster. It must be in the range
        of `service_cidr`.

        Specifying values for `cluster_cidr`, `service_cidr` and `cluster_dns_ip` are permanent and a subsequent change
        requires re-initialisation of the applications. To clarify, system will destroy old `ix-applications` dataset
        and any data within it when any of the values for the above configuration change.

        `node_ip` is the IP address which the kubernetes cluster will assign to the TrueNAS node. It defaults to
        0.0.0.0 and the cluster in this case will automatically manage which IP address to use for managing traffic
        for default NAT network.

        By default kubernetes pods will be using default gateway of the system for outward traffic. This might
        not be desirable for certain users who want to separate NAT traffic over a specific interface / route. System
        will create a L3 network which will be routing the traffic towards default gateway for NAT.

        If users want to restrict traffic over a certain gateway / interface, they can specify a default route
        for the NAT traffic. `route_v4_interface` and `route_v4_gateway` will set a default route for the kubernetes
        cluster IPv4 traffic. Similarly `route_v6_interface` and 'route_v6_gateway` can be used to specify default
        route for IPv6 traffic.
        """
        old_config = await self.config()
        old_config.pop('dataset')
        config = old_config.copy()
        config.update(data)

        await self.validate_data(config, 'kubernetes_update')

        if len(set(old_config.items()) ^ set(config.items())) > 0:
            config['cni_config'] = {}
            await self.middleware.call('datastore.update', self._config.datastore, old_config['id'], config)
            await self.middleware.call('kubernetes.status_change')
            if not config['pool'] and config['pool'] != old_config['pool']:
                # We only want to do this when we don't have any pool configured and would like to use
                # host catalog repos temporarily. Otherwise, we should call this after k8s datasets have
                # been initialised
                await self.middleware.call('catalog.sync_all')

        return await self.config()

    @accepts()
    async def bindip_choices(self):
        """
        Returns ip choices for Kubernetes service to use.
        """
        return {
            d['address']: d['address'] for d in await self.middleware.call(
                'interface.ip_in_use', {'static': True, 'any': True}
            )
        }

    @private
    async def validate_k8s_setup(self):
        k8s_config = await self.middleware.call('kubernetes.config')
        if not k8s_config['dataset']:
            raise CallError('Please configure kubernetes pool.')
        if not await self.middleware.call('service.started', 'kubernetes'):
            raise CallError('Kubernetes service is not running.')

    @accepts()
    async def node_ip(self):
        """
        Returns IP used by kubernetes which kubernetes uses to allow incoming connections.
        """
        k8s_node_config = await self.middleware.call('k8s.node.config')
        node_ip = None
        if k8s_node_config['node_configured']:
            node_ip = next(
                (addr['address'] for addr in k8s_node_config['status']['addresses'] if addr['type'] == 'InternalIP'),
                None
            )
        if not node_ip:
            node_ip = (await self.middleware.call('kubernetes.config'))['node_ip']

        return node_ip


async def setup(middleware):
    await middleware.call(
        'interface.register_listen_delegate',
        ConfigServiceListenSingleDelegate(middleware, 'kubernetes', 'node_ip'),
    )
