import logging
from abc import ABCMeta, abstractmethod
from typing import Dict, List, Set, override

from ray.autoscaler._private.node_launcher import BaseNodeLauncher
from ray.autoscaler.node_provider import NodeProvider as NodeProviderV1
from ray.autoscaler.tags import TAG_RAY_USER_NODE_TYPE
from ray.autoscaler.v2.instance_manager.config import NodeProviderConfig
from ray.core.generated.instance_manager_pb2 import Instance, InstanceType

logger = logging.getLogger(__name__)


class NodeProvider(metaclass=ABCMeta):
    """NodeProvider defines the interface for
    interacting with cloud provider, such as AWS, GCP, Azure, etc.
    """

    @abstractmethod
    def create_nodes(self, instance_type: InstanceType, count: int) -> List[str]:
        """Create new nodes synchronously, returns all non-terminated nodes in the cluster.
        Note that create_nodes could fail partially.
        """
        pass

    @abstractmethod
    def async_terminate_nodes(self, cloud_instance_ids: List[str]) -> None:
        """
        Terminate nodes asynchronously, returns immediately."""
        pass

    @abstractmethod
    def get_non_terminated_nodes(
        self,
    ) -> Dict[str, Instance]:
        """Get all non-terminated nodes in the cluster"""
        pass

    @abstractmethod
    def get_nodes_by_cloud_id(
        self,
        cloud_instance_ids: List[str],
    ) -> Dict[str, Instance]:
        """Get nodes by node ids, including terminated nodes"""
        pass

    @abstractmethod
    def is_readonly(self) -> bool:
        return False


class NodeProviderAdapter(NodeProvider):
    """
    Warps a NodeProviderV1 to a NodeProvider.
    """

    def __init__(
        self,
        provider: NodeProviderV1,
        node_launcher: BaseNodeLauncher,
        instance_config_provider: NodeProviderConfig,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._node_launcher = node_launcher
        self._config = instance_config_provider

    def _filter_instances(
        self,
        instances: Dict[str, Instance],
        instance_ids_filter: Set[str],
        instance_states_filter: Set[int],
    ) -> Dict[str, Instance]:
        filtered = {}
        for instance_id, instance in instances.items():
            if instance_ids_filter and instance_id not in instance_ids_filter:
                continue
            if instance_states_filter and instance.state not in instance_states_filter:
                continue
            filtered[instance_id] = instance
        return filtered

    @override
    def create_nodes(self, instance_type: InstanceType, count: int) -> List[Instance]:
        result = self._node_launcher.launch_node(
            self._config.get_node_config(instance_type.name),
            count,
            instance_type.name,
        )
        # TODO: we should handle failures where the instance type is
        # not available
        if result:
            return [
                self._get_instance(cloud_instance_id)
                for cloud_instance_id in result.keys()
            ]
        return []

    @override
    def async_terminate_nodes(self, clould_instance_ids: List[str]) -> None:
        self._provider.terminate_node(clould_instance_ids)

    @override
    def is_readonly(self) -> bool:
        return self._provider.is_readonly()

    @override
    def get_non_terminated_nodes(self):
        clould_instance_ids = self._provider.non_terminated_nodes()
        return self.get_nodes_by_id(clould_instance_ids)

    @override
    def get_nodes_by_cloud_id(
        self,
        cloud_instance_ids: List[str],
    ) -> Dict[str, Instance]:
        instances = {}
        for cloud_instance_id in cloud_instance_ids:
            instances[cloud_instance_id] = self._get_instance(cloud_instance_id)
        return instances

    def _get_instance(self, cloud_instance_id: str) -> Instance:
        instance = Instance()
        instance.cloud_instance_id = cloud_instance_id
        if self._provider.is_running(cloud_instance_id):
            instance.state = Instance.STARTING
        elif self._provider.is_terminated(cloud_instance_id):
            instance.state = Instance.STOPPED
        else:
            instance.state = Instance.INSTANCE_STATUS_UNSPECIFIED
        instance.interal_ip = self._provider.internal_ip(cloud_instance_id)
        instance.external_ip = self._provider.external_ip(cloud_instance_id)
        instance.instance_type = self._provider.node_tags(cloud_instance_id)[
            TAG_RAY_USER_NODE_TYPE
        ]
        return instance
