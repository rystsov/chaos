from enum import Enum
import sys
import json
from sh import mkdir, rm
import traceback
from confluent_kafka import Consumer, TopicPartition, OFFSET_BEGINNING
from chaos.checks.result import Result
from chaos.workloads.writes.log_utils import State, cmds, transitions, phantoms
import logging
from time import sleep
import os

logger = logging.getLogger("consistency")

class Write:
    def __init__(self):
        self.key = None
        self.op = None
        self.offset = None
        self.started = None
        self.finished = None
        self.max_offset = None

class LogPlayer:
    def __init__(self, config):
        self.config = config
        self.curr_state = dict()
        self.ts_us = None
        self.has_violation = False
        
        self.first_offset = sys.maxsize
        self.last_offset = 0
        self.max_offset = -1
        self.last_write = dict()
        self.key = dict()
        self.ok_writes = dict()
        self.err_writes = dict()
    
    def reread_and_check(self):
        if self.has_violation:
            return

        c = Consumer({
            "bootstrap.servers": self.config["brokers"],
            "enable.auto.commit": False,
            "group.id": "group1",
            "topic.metadata.refresh.interval.ms": 5000, # default: 300000
            "metadata.max.age.ms": 10000, # default: 900000
            "topic.metadata.refresh.fast.interval.ms": 250, # default: 250
            "topic.metadata.propagation.max.ms": 10000, # default: 30000
            "socket.timeout.ms": 10000, # default: 60000
            "connections.max.idle.ms": 0, # default: 0
            "reconnect.backoff.ms": 100, # default: 100
            "reconnect.backoff.max.ms": 10000, # default: 10000
            "statistics.interval.ms": 0, # default: 0
            "api.version.request.timeout.ms": 10000, # default: 10000
            "api.version.fallback.ms": 0, # default: 0
            "fetch.wait.max.ms": 500, # default: 0
            "isolation.level": "read_committed"
        })

        c.assign([TopicPartition(self.config["topic"], 0, OFFSET_BEGINNING)])

        RETRIES=5
        retries=RETRIES

        prev_offset = -1
        is_active = True
        while is_active:
            if retries==0:
                raise Exception("Can't connect to the redpanda cluster")
            msgs = c.consume(timeout=10)
            if len(msgs)==0:
                retries-=1
                sleep(5)
                continue
            for msg in msgs:
                retries=RETRIES
                if msg is None:
                    continue
                if msg.error():
                    logger.debug("Consumer error: {}".format(msg.error()))
                    continue

                offset = msg.offset()

                if offset <= prev_offset:
                    logger.error(f"offsets must increase; observed {offset} after {prev_offset}")
                    self.has_violation = True
                prev_offset = offset

                if offset<self.first_offset:
                    continue

                op = int(msg.value().decode('utf-8'))
                key = msg.key().decode('utf-8')

                if offset in self.ok_writes:
                    write = self.ok_writes[offset]
                    if write.op != op:
                        logger.error(f"read message {key}={op}@{offset} doesn't match written message {write.key}={write.op}@{offset}")
                        self.has_violation = True
                    if write.key != key:
                        logger.error(f"read message {key}={op}@{offset} doesn't match written message {write.key}={write.op}@{offset}")
                        self.has_violation = True
                    del self.ok_writes[offset]
                    if op in self.err_writes:
                        raise Exception("wat")
                elif op in self.err_writes:
                    write = self.err_writes[op]
                    if write.key != key:
                        logger.error(f"read message {key}={op}@{offset} doesn't match written message {write.key}={write.op}")
                        self.has_violation = True
                    if offset <= write.max_offset:
                        logger.error(f"message got lesser offset that was known ({write.max_offset}) before it's written: {write.key}={write.op}@{offset}")
                        self.has_violation = True
                    del self.err_writes[op]
                else:
                    logger.error(f"read unknown message {key}={op}@{offset}")
                    self.has_violation = True

                if offset >= self.last_offset:
                    is_active = False
                    break
        c.close()

        if len(self.ok_writes) != 0:
            self.has_violation = True
            for offset in self.ok_writes:
                write = self.ok_writes[offset]
                logger.error(f"lost message found {write.key}={write.op}@{offset}")
    
    def writing_apply(self, thread_id, parts):
        if self.curr_state[thread_id] == State.SENDING:
            write = Write()
            write.key = self.key[thread_id]
            write.op = int(parts[3])
            write.started = self.ts_us
            write.max_offset = self.max_offset
            self.last_write[thread_id] = write
        elif self.curr_state[thread_id] == State.OK:
            offset = int(parts[3])
            self.first_offset = min(self.first_offset, offset)
            self.last_offset = max(self.last_offset, offset)
            write = self.last_write[thread_id]
            self.last_write[thread_id] = None
            write.offset = offset
            write.finished = self.ts_us
            if offset <= write.max_offset:
                self.has_violation = True
                logger.error(f"message got lesser offset that was known ({write.max_offset}) before it's written: {write.key}={write.op}@{offset}")
            self.max_offset = max(self.max_offset, offset)
            if offset in self.ok_writes:
                known = self.ok_writes[offset]
                logger.error(f"message got already assigned offset: {write.key}={write.op} vs {known.key}={known.op} @ {offset}")
                self.has_violation = True
            self.ok_writes[offset] = write
        elif self.curr_state[thread_id] in [State.ERROR, State.TIMEOUT]:
            if thread_id in self.last_write and self.last_write[thread_id] != None:
                write = self.last_write[thread_id]
                self.last_write[thread_id] = None
                write.offset = None
                write.finished = self.ts_us
                self.err_writes[write.op] = write
    
    def apply(self, line):
        parts = line.rstrip().split('\t')

        if parts[2] not in cmds:
            raise Exception(f"unknown cmd \"{parts[2]}\"")

        if self.ts_us == None:
            self.ts_us = int(parts[1])
        else:
            delta_us = int(parts[1])
            self.ts_us = self.ts_us + delta_us
        
        new_state = cmds[parts[2]]

        if new_state == State.EVENT:
            return
        if new_state == State.VIOLATION:
            self.has_violation = True
            logger.error(parts[3])
            return
        if new_state == State.LOG:
            return
        
        thread_id = int(parts[0])
        if thread_id not in self.curr_state:
            self.curr_state[thread_id] = None
        if self.curr_state[thread_id] == None:
            if new_state != State.STARTED:
                raise Exception(f"first logged command of a new thread should be started, got: \"{parts[2]}\"")
            self.curr_state[thread_id] = new_state
            self.key[thread_id] = parts[3]
        else:
            if new_state not in transitions[self.curr_state[thread_id]]:
                raise Exception(f"unknown transition {self.curr_state[thread_id]} -> {new_state}")
            self.curr_state[thread_id] = new_state

        self.writing_apply(thread_id, parts)

def validate(config, workload_dir):
    logger.setLevel(logging.DEBUG)
    logger_handler_path = os.path.join(workload_dir, "consistency.log")
    handler = logging.FileHandler(logger_handler_path)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    logger.addHandler(handler)

    has_error = True
    
    try:
        has_violation = False

        if len(config["workload"]["nodes"]) != 1:
            raise Exception("can't validate more than one workload nodes")

        for node in config["workload"]["nodes"]:
            player = LogPlayer(config)
            with open(os.path.join(workload_dir, node, "workload.log"), "r") as workload_file:
                last_line = None
                for line in workload_file:
                    if last_line != None:
                        player.apply(last_line)
                    last_line = line
            player.reread_and_check()
            has_violation = has_violation or player.has_violation
        
        has_error = has_violation

        return {
            "result": Result.FAILED if has_violation else Result.PASSED
        }
    except:
        e, v = sys.exc_info()[:2]
        trace = traceback.format_exc()
        logger.debug(v)
        logger.debug(trace)
        
        return {
            "result": Result.UNKNOWN
        }
    finally:
        handler.flush()
        handler.close()
        logger.removeHandler(handler)

        if not has_error:
            rm("-rf", logger_handler_path)