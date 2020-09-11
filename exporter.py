import json
from datetime import datetime
import os
import sys
import subprocess
import importlib
import time
import uuid
import dateutil.parser
import traceback
import logging as lg
from apscheduler.schedulers.asyncio import AsyncIOScheduler

sys.path.append(os.path.join(os.path.dirname(sys.path[0]),'modules'))
sys.path.append(os.path.join(os.path.dirname(sys.path[0]),'app'))
from qredis import *
from qmetrics import *
from gclog import *
from syshelper import *

try:
	import asyncio
except ImportError:
	import trollius as asyncio

REDIS_PASSWORD = os.environ['REDIS_PASSWORD']
KUBERNETES = os.environ['KUBERNETES']
if (KUBERNETES == 'true'):
	KUBERNETES = True
else:
	KUBERNETES = False

gclog = GCLog({
	"name": "scheduler-log",
	"write_severity": "ALL",
})

redis = QRedis({
	"host": "redis",
	"port": 6379,
	"password": REDIS_PASSWORD
})

metrics = QMetrics()

def str_to_class(module_name, class_name):
	module = importlib.import_module(module_name)
	class_ = getattr(module, class_name)
	return class_

def stop_container(container):
	gclog.write('Stopping hung container: {container}'.format(container=container), 'DEBUG')
	if KUBERNETES:
		cmd = 'kubectl delete pod {container} --force=true'.format(container=container)
	else:
		cmd = 'docker rm -f $(docker ps -a -q --filter name={container} --format="{{{{.ID}}}}")'.format(container=container)
	run_cmd(cmd, shell=True)
	return

def drop_redis_key(key):
	redis.r.delete(key)
	print("Dropped {key} key".format(key=key))
	return 

def run_monitor(scheduler):
	try:
		while True:
			time.sleep(1)

			agents = redis.get_state('JOB_*')
			for agent_key, agent_state in agents.items():
				#print("Last message for {script} seen {diff_seconds} seconds ago".format(script=script, diff_seconds=diff_seconds))
				if (redis.agent_restart_needed(agent_key)):
					redis.restart_agent(agent_key, 'Last update timeout (ongoing monitor)')
					agent_container = redis.get_agent_container(agent_key)
					gclog.write('Force stop agent on service monitoring, agent: {agent_key}, container: {container}'.format(agent_key=agent_key, container=agent_container), 'DEBUG')
					scheduler.add_job(stop_container, None, [agent_container])

					#set new UID for container
					gclog.write('Spawn container on service monitoring for agent: {agent_key}'.format(agent_key=agent_key), 'DEBUG')

					initial_config = json.loads(redis.get_agent_config(agent_key))
					initial_config['container_name'] = uuid.uuid4().hex

					gclog.write({
						"message": "Agent RESTART scheduled",
						"config": initial_config,
					}, 'DEBUG')
					
					scheduler.add_job(spawn_agent, None, [initial_config])
	except Exception as ex:
		gclog.write({
			'message': "[SCHEDULER DEAD]: Scheduler exception of type {0} occurred. Arguments:\n{1!r}".format(type(ex).__name__, ex.args),
			'error': traceback.format_exc(),
		}, 'ERROR')
		sys.exit(1)

def get_cmd_continuous(config):
	if (KUBERNETES):
		script = "start-container-kubernetes.sh"
	else:
		script = "start-container.sh"
	cmd = "scheduler/{script} {modulepy} {exchange} {data} {url} {market} {module} {schedule} {restart} {restart_after} {copy} {container_name}".format(
		script=script,
		modulepy=config['module'].lower(),
		exchange=str(config['exchange']),
		data=str(config['data']),
		url=str(config['url']),
		market=str(config['market']),
		module=str(config['module']),
		schedule=str(config['schedule']),
		restart=str(config['restart']),
		restart_after=str(config['restart_after']),
		copy=str(config['copy']),
		container_name=str(config['container_name'])
	)
	return cmd

def get_cmd_cron(config):
	if (KUBERNETES):
		script = "start-job-kubernetes.sh"
	else:
		script = "start-container.sh"
	cmd = 'scheduler/{script} {modulepy} {exchange} {data} "{url}" {market} {module} {schedule} "{cron}" {threads} {copy} {container_name}'.format(
		script=script,
		modulepy=config['module'].lower(),
		exchange=str(config['exchange']),
		data=str(config['data']),
		url=str(config['url']),
		market=str(config['market']),
		module=str(config['module']),
		schedule=str(config['schedule']),
		cron=str(config['cron']),
		threads=str(config['threads']),
		copy=str(config['copy']),
		container_name=str(config['container_name'])
	)
	return cmd

def spawn_agent(config):
	if (config['schedule'] == 'continuous'):
		cmd = get_cmd_continuous(config)
	if (config['schedule'] == 'cron'):
		cmd = get_cmd_cron(config)

	gclog.write({
		"message": "Spawning new agent",
		"config": config,
		"cmd": cmd,
	}, 'DEBUG')

	run_cmd(cmd, shell=True)

def start_agent(config):
	agent_key = redis.generate_key(config)
	if redis.agent_exists(agent_key):
		if redis.agent_restart_needed(agent_key):
			gclog.write({
				"message": "Agent RESTART scheduled",
				"config": config,
			}, 'DEBUG')
			redis.restart_agent(agent_key, 'Last update timeout (on monitor startup)')
			spawn_agent(config)
		else:
			gclog.write('Agent exists, restart won\'t needed: {agent_key}'.format(agent_key=agent_key), 'DEBUG')

	else:
		if (config['schedule'] == 'continuous'):
			gclog.write('Spawn container on service startup for agent: {agent_key}'.format(agent_key=agent_key), 'DEBUG')
		if (config['schedule'] == 'cron'):
			#new container name
			config['container_name'] = uuid.uuid4().hex #unique container name
			#gclog.write('Spawn container on cron trigger for agent: {agent_key}'.format(agent_key=agent_key), 'DEBUG')
		spawn_agent(config)
	return 

def pushgateway_monitor():
	keys_ttl = 5.5#how long should keys in pushgateway live
	try:
		while True:
			time.sleep(0.5)

			keys = redis.get_dict(redis.pg_keys_list)
			now = datetime.datetime.utcnow().timestamp()
			for job_key, job_timestamp in keys.items():
				job_key = job_key.decode('utf-8')
				job_timestamp = float(job_timestamp)
				if (now-job_timestamp) >= keys_ttl:
					gclog.write('DELETING PUSHGATEWAY JOB {job_key}'.format(job_key=job_key), 'DEBUG')
					redis.del_dict_key(redis.pg_keys_list, job_key)
					metrics.delete(job_key)


	except Exception as ex:
		gclog.write({
			'message': "[PUSHGATEWAY DEAD]: Monitor exception of type {0} occurred. Arguments:\n{1!r}".format(type(ex).__name__, ex.args),
			'error': traceback.format_exc(),
		}, 'ERROR')
		sys.exit(1)


if __name__ == '__main__':

	def main():

		scheduler = AsyncIOScheduler()
		scheduler.configure({
			'apscheduler.executors.default': {
				'class': 'apscheduler.executors.pool:ThreadPoolExecutor',
				'max_workers': '1000'
			},
			'apscheduler.executors.processpool': {
				'type': 'processpool',
				'max_workers': '1000'
			},
			'apscheduler.timezone': 'UTC',
		})

		#lg.basicConfig()
		#lg.getLogger('apscheduler').setLevel(lg.DEBUG)



		#running monitor first
		scheduler.add_job(run_monitor, None, [scheduler])

		#pushgateway monitor
		scheduler.add_job(pushgateway_monitor, None, [])

		config_path = os.path.join(os.path.dirname(__file__), './config.json')

		with open(config_path) as f:
			config = json.load(f)

		#spawning X copies of the same process
		default_agent_copies = config['default_agent_copies']
		jobs = []
		for item in config['jobs']:
			i = 1
			if 'default_agent_copies' in item:
				copies_to_spawn = item['default_agent_copies']
			else:
				copies_to_spawn = default_agent_copies
			while i <= copies_to_spawn:
				item['container_name'] = uuid.uuid4().hex #unique container name
				item['copy'] = i #number of agent copy
				jobs.append(item.copy())
				i = i +1

		#get list of redis-running agents
		r_agents_keys = []
		agents = redis.get_state('JOB_*')
		for agent_key, agent_state in agents.items():
			r_agents_keys.append(agent_key.decode("utf-8"))

		#get list of agents to be loaded
		c_agents_keys = []
		for job in jobs:
			job_key = redis.generate_key(job)
			c_agents_keys.append(job_key)

		#drop zomby-keys left from previous monitor
		for r_agent_key in r_agents_keys:
			if r_agent_key not in c_agents_keys:
				zomby_container = redis.get_agent_container(r_agent_key)
				gclog.write('Zomby-Agent detected in Redis, agent: {r_agent_key}, container: {zomby_container}'.format(r_agent_key=r_agent_key, zomby_container=zomby_container), 'DEBUG')
				scheduler.add_job(stop_container, None, [zomby_container])
				drop_redis_key(r_agent_key)

		#executing jobs
		for job in jobs:
			if job['schedule'] == 'cron':
				#parse crontab expression with seconds parameter (6 params required)
				cron = job['cron'].split('_')
				scheduler.add_job(
					start_agent,
					'cron', 
					second=cron[0],
					minute=cron[1],
					hour=cron[2],
					day=cron[3],
					month=cron[4],
					year=cron[5],
					args=[job]
				)
			else:
				scheduler.add_job(start_agent, None, [job])

		scheduler.start()
		print('Press Ctrl+{0} to exit'.format('Break' if os.name == 'nt' else 'C'))

		gclog.write('[SCHEDULER ALIVE]', 'DEBUG')

		# Execution will block here until Ctrl+C (Ctrl+Break on Windows) is pressed.
		try:
			asyncio.get_event_loop().run_forever()
		except (KeyboardInterrupt, SystemExit):
			pass

	try:
		main()
	except Exception as ex:
		gclog.write({
			'message': "[SCHEDULER DEAD]: Scheduler exception of type {0} occurred. Arguments:\n{1!r}".format(type(ex).__name__, ex.args),
			'error': traceback.format_exc(),
		}, 'ERROR')
		sys.exit(1)