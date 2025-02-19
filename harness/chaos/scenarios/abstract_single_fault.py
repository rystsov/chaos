from abc import ABC, abstractmethod
import os
from sh import scp, mkdir, rm
import json
import sh
import time
from chaos.checks.all import CHECKS
from chaos.faults.all import FAULTS
from chaos.faults.types import FaultType
from chaos.workloads.all import WORKLOADS, wait_all_workloads_killed
from time import sleep
from chaos.checks.result import Result
import copy
from chaos.types import TimeoutException
import sys
import traceback

import logging

from chaos.redpanda_static_cluster import RedpandaCluster

logger = logging.getLogger("chaos")

class AbstractSingleFault(ABC):
    SUPPORTED_WORKLOADS = set()
    SUPPORTED_FAULTS = set()
    SUPPORTED_CHECKS = set()

    def __init__(self):
        self.redpanda_cluster = None
        self.workload_cluster = None
        self.fault = None
        self.config = None
        self.is_workload_log_fetched = False
        self.is_redpanda_log_fetched = False

    def normalize_fault(self, fault_config):
        if fault_config == None:
            return None
        if isinstance(fault_config, str):
            return {
                "name": fault_config
            }
        elif isinstance(fault_config, dict):
            return fault_config
        else:
            raise Exception(f"unknown fault type: {type(fault_config)}")
    
    def validate(self, config):
        if config["workload"]["name"] not in self.SUPPORTED_WORKLOADS:
            raise Exception(f"unknown workload: {config['workload']}")
        if config["fault"] != None:
            fault = self.normalize_fault(config["fault"])
            if fault["name"] not in self.SUPPORTED_FAULTS:
                raise Exception(f"unknown fault: {fault['name']}")
        for check in config["checks"]:
            if check["name"] not in self.SUPPORTED_CHECKS:
                raise Exception(f"unknown check: {check['name']}")
            if check["name"] == "progress_during_fault":
                if "selector" in check:
                    if check["selector"] not in ["any", "all"]:
                        raise Exception(f"unknown selector value for progress_during_fault: {check['selector']}")
                fault = self.normalize_fault(config["fault"])
                if fault == None:
                    raise Exception(f"progress_during_fault works only with faults, found None")
                fault = FAULTS[fault["name"]](fault)
                if fault.fault_type != FaultType.RECOVERABLE:
                    raise Exception(f"progress_during_fault works only with {FaultType.RECOVERABLE} faults, found {fault.fault_type}")
    
    def save_config(self):
        with open(f"/mnt/vectorized/experiments/{self.config['experiment_id']}/info.json", "w") as info:
            info.write(json.dumps(self.config, indent=2))
    
    def fetch_workload_logs(self):
        if self.workload_cluster != None:
            if self.is_workload_log_fetched:
                return
            logger.info(f"stopping workload everywhere")
            try:
                self.workload_cluster.stop_everywhere()
            except:
                pass
            self.workload_cluster.kill_everywhere()
            self.workload_cluster.wait_killed(timeout_s=10)
            for node in self.workload_cluster.nodes:
                try:
                    logger.info(f"fetching oplog from {node.ip}")
                    mkdir("-p", f"/mnt/vectorized/experiments/{self.config['experiment_id']}/{node.ip}")
                    scp(f"ubuntu@{node.ip}:/mnt/vectorized/workloads/logs/{self.config['experiment_id']}/{node.ip}/workload.log",
                    f"/mnt/vectorized/experiments/{self.config['experiment_id']}/{node.ip}/workload.log")
                    scp(f"ubuntu@{node.ip}:/mnt/vectorized/workloads/logs/system.log",
                        f"/mnt/vectorized/experiments/{self.config['experiment_id']}/{node.ip}/system.log")
                except:
                    pass
            self.is_workload_log_fetched = True
    
    def fetch_redpanda_logs(self):
        if self.redpanda_cluster != None:
            if self.is_redpanda_log_fetched:
                return
            logger.info(f"stopping redpanda")
            self.redpanda_cluster.kill_everywhere()
            self.redpanda_cluster.wait_killed(timeout_s=10)
            mkdir("-p", f"/mnt/vectorized/experiments/{self.config['experiment_id']}/redpanda")
            for node in self.redpanda_cluster.nodes:
                mkdir("-p", f"/mnt/vectorized/experiments/{self.config['experiment_id']}/redpanda/{node.ip}")
                logger.info(f"fetching logs from {node.ip}")
                scp(
                    f"ubuntu@{node.ip}:/mnt/vectorized/redpanda/log.*",
                    f"/mnt/vectorized/experiments/{self.config['experiment_id']}/redpanda/{node.ip}/")
            self.is_redpanda_log_fetched = True
    
    def remove_logs(self):
        for node in self.workload_cluster.nodes:
            rm("-rf", f"/mnt/vectorized/experiments/{self.config['experiment_id']}/{node.ip}/workload.log")
        rm("-rf", f"/mnt/vectorized/experiments/{self.config['experiment_id']}/redpanda")
    
    def get_progress_during_fault(self):
        for check_config in self.config["checks"]:
            if check_config["name"] == "progress_during_fault":
                if "selector" not in check_config:
                    check_config["selector"] = "all"
                return check_config
        return None
    
    def _reconfigure(self, replicas, topic, partition=0, namespace="kafka", timeout_s=10):
        logger.info(f"reconfiguring {namespace}/{topic}")
        info = self.redpanda_cluster.wait_details(topic, partition=partition, namespace=namespace, timeout_s=timeout_s)
        is_target_node_id = {node.id: True for node in replicas}
        is_same = len(info.replicas) == len(replicas)
        for node in info.replicas:
            if node.id not in is_target_node_id:
                is_same = False
        if is_same:
            return
        controller = self.redpanda_cluster.wait_leader("controller", namespace="redpanda", timeout_s=timeout_s)
        self.redpanda_cluster.reconfigure(controller, replicas, topic, partition=partition, namespace=namespace)
        begin = time.time()
        while True:
            if time.time() - begin > timeout_s:
                raise TimeoutException(f"can't reconfigure {topic} within {timeout_s} sec")
            replicas_info = self.redpanda_cluster.wait_details(topic, partition=partition, namespace=namespace, timeout_s=timeout_s)
            if replicas_info.status == "done":
                is_same = len(replicas_info.replicas) == len(replicas)
                for node in replicas_info.replicas:
                    if node.id not in is_target_node_id:
                        is_same = False
                if is_same:
                    break
            time.sleep(1)
    
    def _transfer(self, new_leader, topic, partition=0, namespace="kafka", timeout_s=10):
        old_leader = self.redpanda_cluster.wait_leader(topic, namespace=namespace, timeout_s=timeout_s)
        logger.debug(f"{namespace}/{topic}/{partition} leader: {old_leader.ip} (id={old_leader.id})")
        if new_leader != old_leader:
            begin = time.time()
            while True:
                if time.time() - begin > timeout_s:
                    raise TimeoutException(f"can't transfer leader of {topic} to {new_leader.ip} within {timeout_s} sec")
                try:
                    self.redpanda_cluster.transfer_leadership_to(new_leader, namespace, topic, partition)
                    break
                except:
                    e, v = sys.exc_info()[:2]
                    trace = traceback.format_exc()
                    logger.error(e)
                    logger.error(v)
                    logger.error(trace)
                    sleep(1)
            self.redpanda_cluster.wait_leader_is(new_leader, namespace, topic, partition, timeout_s=timeout_s)
            logger.debug(f"{namespace}/{topic}/{partition} leader: {new_leader.ip} (id={new_leader.id})")

    def read_config(self, path, default):
        root = self.config
        for node in path:
            if node not in root:
                return default
            root = root[node]
        return root

    @abstractmethod
    def prepare_experiment(self, config, experiment_id):
        pass
    
    def measure_experiment(self):
        logger.info(f"start measuring")
        for node in self.workload_cluster.nodes:
            self.workload_cluster.emit_event(node, "measure")

        if self.fault == None:
            steady_s = self.read_config(["settings", "steady_s"], 180)
            if steady_s > 0:
                logger.info(f"wait for {steady_s} seconds to record steady state")
                sleep(steady_s)
        elif self.fault.fault_type==FaultType.RECOVERABLE:
            steady_s = self.read_config(["settings", "steady_s"], 60)
            if steady_s > 0:
                logger.info(f"wait for {steady_s} seconds to record steady state")
                sleep(steady_s)
            for node in self.workload_cluster.nodes:
                self.workload_cluster.emit_event(node, "injecting")
            logger.info(f"injecting {self.fault.name}")
            self.fault.inject(self)
            logger.info(f"injected {self.fault.name}")
            for node in self.workload_cluster.nodes:
                self.workload_cluster.emit_event(node, "injected")
            after_fault_info = {}
            for node in self.workload_cluster.nodes:
                after_fault_info[node.ip] = self.workload_cluster.info(node)
            impact_s = self.read_config(["settings", "impact_s"], 60)
            if impact_s > 0:
                logger.info(f"wait for {impact_s} seconds to record impacted state")
                sleep(impact_s)
            before_heal_info = {}
            for node in self.workload_cluster.nodes:
                before_heal_info[node.ip] = self.workload_cluster.info(node)
            progress_during_fault = self.get_progress_during_fault()
            if progress_during_fault != None:
                progress_during_fault["result"] = Result.PASSED
                has_any = False
                has_all = True
                for ip in before_heal_info.keys():
                    delta = before_heal_info[ip].succeeded_ops - after_fault_info[ip].succeeded_ops
                    progress_during_fault[ip] = {
                        "delta": delta
                    }
                    if delta < progress_during_fault["min-delta"]:
                        has_all = False
                        progress_during_fault[ip]["result"] = Result.HANG
                    else:
                        has_any = True
                        progress_during_fault[ip]["result"] = Result.PASSED
                if progress_during_fault["selector"] == "all" and not has_all:
                    progress_during_fault["result"] = Result.HANG
                if progress_during_fault["selector"] == "any" and not has_any:
                    progress_during_fault["result"] = Result.HANG
                self.config["result"] = Result.more_severe(
                    self.config["result"],
                    progress_during_fault["result"]
                )
                self.save_config()
            for node in self.workload_cluster.nodes:
                self.workload_cluster.emit_event(node, "healing")
            logger.info(f"healing {self.fault.name}")
            self.fault.heal(self)
            logger.info(f"healed {self.fault.name}")
            for node in self.workload_cluster.nodes:
                self.workload_cluster.emit_event(node, "healed")
            recovery_s = self.read_config(["settings", "recovery_s"], 60)
            if recovery_s > 0:
                logger.info(f"wait for {recovery_s} seconds to record recovering state")
                sleep(recovery_s)
        elif self.fault.fault_type==FaultType.ONEOFF:
            steady_s = self.read_config(["settings", "steady_s"], 60)
            if steady_s > 0:
                logger.info(f"wait for {steady_s} seconds to record steady state")
                sleep(steady_s)
            for node in self.workload_cluster.nodes:
                self.workload_cluster.emit_event(node, "injecting")
            logger.info(f"injecting {self.fault.name}")
            self.fault.execute(self)
            logger.info(f"injected {self.fault.name}")
            for node in self.workload_cluster.nodes:
                self.workload_cluster.emit_event(node, "injected")
            recovery_s = self.read_config(["settings", "recovery_s"], 120)
            if recovery_s > 0:
                logger.info(f"wait for {recovery_s} seconds to record recovering / impacted state")
                sleep(recovery_s)
        else:
            raise Exception(f"Unknown fault type {self.fault.fault_type}")

        self.fetch_workload_logs()

        for check_cfg in self.config["checks"]:
            if check_cfg["name"] == "progress_during_fault":
                continue
            check = CHECKS[check_cfg["name"]]
            result = check().check(self)
            for key in result:
                check_cfg[key] = result[key]
            self.config["result"] = Result.more_severe(self.config["result"], check_cfg["result"])
        self.save_config()

        self.config = self.workload_cluster.analyze(copy.deepcopy(self.config))
        logger.info(f"experiment {self.config['experiment_id']} result: {self.config['result']}")
        self.save_config()

        if self.config["result"] == Result.FAILED:
            if "exit_on_violation" in self.config:
                if self.config["exit_on_violation"]:
                    os._exit(42)
        
        self.fetch_redpanda_logs()

        if "settings" in self.config:
            if "remove_logs_on_success" in self.config["settings"]:
                if self.config["settings"]["remove_logs_on_success"]:
                    if self.config["result"]==Result.PASSED:
                        self.remove_logs()
    
    def execute(self, config, experiment_id):
        try:
            self.prepare_experiment(config, experiment_id)
            self.measure_experiment()
            return self.config
        except:
            self.config["result"] = Result.more_severe(self.config["result"], Result.UNKNOWN)
            self.save_config()
            raise
        finally:
            try:
                self.fetch_workload_logs()
            except:
                pass
            try:
                self.fetch_redpanda_logs()
            except:
                pass