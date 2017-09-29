import asyncio
import configparser
import json
from time import sleep

from openzwave.network import ZWaveController, ZWaveNetwork, ZWaveNode, dispatcher
from openzwave.option import ZWaveOption

from Firefly import logging, scheduler
from Firefly.const import SERVICE_CONFIG_FILE, ZWAVE_FILE
from Firefly.helpers.events import Command
from Firefly.helpers.service import Service

PACKAGE_MAPPING = {
  'ZW096 Smart Switch 6':                'zwave_aeotec_smart_switch_gen_6',
  'DSC06106 Smart Energy Switch':        'zwave_aeotec_dsc06106_smart_switch',

  'ZW100 MultiSensor 6':                 'aeotec.zw100_multisensor_6',
  'ZW120 Door Window Sensor Gen5':       'aeotec.zw120_door_window_sensor_gen5',
  'Unknown: type=0004, id=0002':         'zwave_generic_devices.contact_senso',

  'ZW112 Door Window Sensor 6':          'zwave_aeotec_door_window_gen_6',
  'ZW097 Dry Contact Sensor Gen5':       'zwave_aeotec_zw097_dry_contact',
  'DSB45 Water Sensor':                  'zwave_aeotec_dsb45_water',
  '12730 Fan Control Switch':            'zwave_ge_12724_dimmer',
  '12729 3-Way Dimmer Switch':           'zwave_ge_12724_dimmer',
  '12724 3-Way Dimmer Switch':           'zwave_ge_12724_dimmer',
  '14294 In-Wall Smart Dimmer':          'zwave_ge_12724_dimmer',
  '12727 In-Wall Smart Switch (Toggle)': 'zwave_switch',
  'DSB05 Multisensor':                   'zwave_aeotec_dsb05_multi_sensor',
  'DSC11 Smart Strip':                   'zwave_aeotec_dsc11_smart_strip',
  'ZW080 Siren Gen5':                    'zwave_aeotec_zw080',

  'ZW111 Nano Dimmer':                   'zwave_aeotec_zw111_dimmer'
}
CONFIG_MAPPING = {
  'ge/12724-dimmer.xml': 'zwave_ge_12724_dimmer'
}

'''
The ZWAVE service is the background service for zwave.

There will be a zwave device class in the zwave device folder. This class will handle actions
like sending commands to the zwave service. This by using this device class we can easily make
new zwave devices.

Zwave service should automatically create child devices from the device type that the zwave
service detects from that node ID. These devices can later be customized to custom device types.

All zwave devices will be IDed by their serial number if possible but will also have to keep tack
of what node number they are for sending commands.
'''

TITLE = 'Z-Wave service for Firefly'
AUTHOR = 'Zachary Priddy me@zpriddy.com'
SERVICE_ID = 'service_zwave'
COMMANDS = ['send_command', 'stop', 'add_node', 'cancel']
REQUESTS = ['get_nodes', 'get_orphans']

SECTION = 'ZWAVE'
# TODO: Make this 300 after testing is done
STARTUP_TIMEOUT = 10


def Setup(firefly, package, **kwargs):
  logging.message('Setting up %s service' % SERVICE_ID)
  config = configparser.ConfigParser()
  config.read(SERVICE_CONFIG_FILE)

  enable = config.getboolean(SECTION, 'enable', fallback=False)
  port = config.get(SECTION, 'port', fallback=None)
  path = config.get(SECTION, 'path', fallback='/opt/firefly_system/python-openzwave/openzwave/config')
  # path = config.get(SECTION, 'path', fallback=None)
  security = config.getboolean(SECTION, 'security', fallback=True)
  if not enable or port is None:
    return False

  try:
    with open(ZWAVE_FILE) as f:
      zc = json.loads(f.read())
      installed_nodes = zc.get('installed_nodes')
      if installed_nodes:
        kwargs['installed_nodes'] = installed_nodes
      logging.message('ZWAVE DEBUG - INSTALLED NODES: %s' % str(installed_nodes))
  except Exception as e:
    logging.error(code='FF.ZWA.SET.001', args=(e))  # error reading zwave.json file
    pass

  zwave = Zwave(firefly, package, enable=enable, port=port, path=path, security=security, **kwargs)
  firefly.components[SERVICE_ID] = zwave
  return True


class Zwave(Service):
  def __init__(self, firefly, package, **kwargs):
    super().__init__(firefly, SERVICE_ID, package, TITLE, AUTHOR, COMMANDS, REQUESTS)

    self._port = kwargs.get('port')
    self._path = kwargs.get('path')
    self._enable = kwargs.get('enable')
    self._zwave_option = None
    self._network: ZWaveNetwork = None
    self._installed_nodes = kwargs.get('installed_nodes', {})

    # TODO: Enable zwave security
    self._security_enable = kwargs.get('security')

    self.add_command('send_command', self.send_command)
    self.add_command('stop', self.stop)
    self.add_command('add_node', self.add_device)
    self.add_command('cancel', self.cancel_command)

    self.add_request('get_nodes', self.get_nodes)
    self.add_request('get_orphans', self.get_orphans)

    self.new_alias = None
    self.healed = False

    scheduler.runInS(5, self.initialize_zwave)

    scheduler.runEveryM(10, self.poll_nodes)

  async def initialize_zwave(self):
    if self._network is not None:
      return False
    logging.info('Trying to open port for ZWave...')
    try:
      self._zwave_option = ZWaveOption(self._port, self._path)
      self._zwave_option.set_console_output(False)
      self._zwave_option.lock()

      self._network = ZWaveNetwork(self._zwave_option)  # , autostart=False)
      # self._network.start()

    except Exception as e:
      logging.error(code='FF.ZWA.INI.001', args=e)  # error opening zwave port. are you running as sudo? is the port correct? error: %s
      self._enable = False
      return

    logging.message('Starting ZWAVE - This can take up to 5 minutes.. Will notify when finished')

    for i in range(STARTUP_TIMEOUT):
      if self._network.state >= self._network.STATE_AWAKED:
        logging.message('ZWAVE is awake')
        break
      else:
        await asyncio.sleep(1)

    for i in range(STARTUP_TIMEOUT):
      if self._network.state >= self._network.STATE_READY:
        logging.message('ZWAVE is ready')
        break
      else:
        await asyncio.sleep(1)

    dispatcher.connect(self.zwave_handler, ZWaveNetwork.SIGNAL_NODE_ADDED)
    # dispatcher.connect(self.zwave_handler, ZWaveNetwork.SIGNAL_BUTTON_OFF)
    # dispatcher.connect(self.zwave_handler, ZWaveNetwork.SIGNAL_BUTTON_ON)
    dispatcher.connect(self.zwave_handler, ZWaveNetwork.SIGNAL_NODE)
    # TODO Maybe comment next line
    #### dispatcher.connect(self.zwave_handler, ZWaveNetwork.SIGNAL_NODE_EVENT)
    ##dispatcher.connect(self.zwave_handler, ZWaveNetwork.SIGNAL_VALUE_ADDED)
    ##dispatcher.connect(self.zwave_handler, ZWaveNetwork.SIGNAL_VALUE_REFRESHED)
    # dispatcher.connect(self.zwave_handler, ZWaveNetwork.SIGNAL_VALUE_CHANGED)
    # dispatcher.connect(self.zwave_handler, ZWaveNetwork.SIGNAL_VALUE)
    # dispatcher.connect(self.zwave_handler, ZWaveNetwork.SIGNAL_ALL_NODES_QUERIED)

    dispatcher.connect(self.zwave_controller_command, ZWaveNetwork.SIGNAL_CONTROLLER_COMMAND)
    # dispatcher.connect(self.zwave_handler, ZWaveNetwork.SIGNAL_SCENE_EVENT)
    # dispatcher.connect(self.zwave_handler, ZWaveNetwork.SIGNAL_GROUP)
    ##dispatcher.connect(self.zwave_handler, ZWaveNetwork.SIGNAL_NODE_QUERIES_COMPLETE)

    ##dispatcher.connect(self.zwave_handler, ZWaveNetwork.SIGNAL_VALUE_CHANGED)
    dispatcher.connect(self.new_node, ZWaveNetwork.SIGNAL_NODE_ADDED)

    dispatcher.connect(self.node_handler, ZWaveNetwork.SIGNAL_NODE)
    dispatcher.connect(self.value_handler, ZWaveNetwork.SIGNAL_VALUE)
    dispatcher.connect(self.value_added_handler, ZWaveNetwork.SIGNAL_VALUE_ADDED)
    dispatcher.connect(self.notification_handler, ZWaveNetwork.SIGNAL_NOTIFICATION)
    dispatcher.connect(self.node_queries_handler, ZWaveNetwork.SIGNAL_NODE_QUERIES_COMPLETE)
    dispatcher.connect(self.essential_queries_handler, ZWaveNetwork.SIGNAL_ESSENTIAL_NODE_QUERIES_COMPLETE)

    # self._network.set_poll_interval(milliseconds=500)

    # Initial refresh of all nodes
    self.zwave_refresh()
    scheduler.runEveryM(10, self.zwave_refresh, job_id='123-zwave_refresh')
    scheduler.runEveryH(10, self.find_dead_nodes, job_id='123-find-dead-nodes')

    for node_id, node in self._network.nodes.items():
      try:
        if str(node.node_id) in self._installed_nodes:
          command = Command(self._installed_nodes[str(node.node_id)], SERVICE_ID, 'ZWAVE_UPDATE', node=node)
          self._firefly.send_command(command)
      except Exception as e:
        logging.error('ZWAVE INIT ERROR: %s' % str(e))

  def find_dead_nodes(self, **kwargs):
    for node_id, node in self._network.nodes.items():
      logging.message('ZWAVE FINDING DEAD NODES: %s' % str(node_id))
      self._network.controller.has_node_failed(node_id)
      sleep(60)

  def zwave_controller_command(self, **kwargs):
    logging.message('ZWAVE CONTROLLER COMMAND: %s' % str(kwargs))

  def node_handler(self, **kwargs):
    '''Called when a node is changed, added, removed'''
    logging.message('ZWAVE NODE HANDLER: %s' % str(kwargs))

  def value_added_handler(self, **kwargs):
    '''Called when a node is changed, added, removed'''
    logging.message('ZWAVE VALUE ADDED HANDLER: %s' % str(kwargs))
    logging.message('ZWAVE VALUE ADDED HANDLER: %s' % str(kwargs['value'].to_dict()))

  def value_handler(self, **kwargs):
    '''Called when a node is changed, added, removed'''
    logging.message('ZWAVE VALUE HANDLER: %s' % str(kwargs))
    logging.message('ZWAVE VALUE HANDLER: %s' % str(kwargs['value'].to_dict()))
    node: ZWaveNode = kwargs.get('node')
    if str(node.node_id) in self._installed_nodes:
      command = Command(self._installed_nodes[str(node.node_id)], SERVICE_ID, 'ZWAVE_UPDATE', node=node, values=kwargs['value'], values_only=True)
      self._firefly.send_command(command)
    else:
      logging.error('node %s not found in installed nodes' % str(node.node_id))
      self.add_child_nodes(**kwargs)

  def essential_queries_handler(self, **kwargs):
    logging.message('ZWAVE ESSENTIAL NODE QUERIES: %s' % str(kwargs))
    # 2017-09-29 02:08:12	DEBUG:	Z-Wave Notification EssentialNodeQueriesComplete : {'notificationType': 'EssentialNodeQueriesComplete', 'homeId': 3348036247, 'nodeId': 43}
    # 2017-09-29 02:08:12	INFO:	ZWAVE ESSENTIAL NODE QUERIES: {'signal': 'EssentialNodeQueriesComplete', 'sender': _Anonymous, 'network': <openzwave.network.ZWaveNetwork object at 0x7458e270>,
    # 'node': <openzwave.node.ZWaveNode object at 0x703c4fd0>} [essential_queries_handler - zwave.py]
    # TODO: Mark devices as queries done
    # if str(node.node_id) in self._installed_nodes:
    #  command = Command(self._installed_nodes[str(node.node_id)], SERVICE_ID, 'ZWAVE_UPDATE', node=node, values=kwargs['value'], values_only=True)
    #  self._firefly.send_command(command)
    # else:
    #  logging.error('node %s not found in installed nodes' % str(node.node_id))

  def node_queries_handler(self, **kwargs):
    logging.message('ZWAVE NODE QUERIES: %s' % str(kwargs))
    # 2017-09-29 02:08:14	DEBUG:	Z-Wave Notification NodeQueriesComplete : {'notificationType': 'NodeQueriesComplete', 'homeId': 3348036247, 'nodeId': 43}
    # 2017-09-29 02:08:14	INFO:	ZWAVE NODE QUERIES: {'signal': 'NodeQueriesComplete', 'sender': _Anonymous, 'network': <openzwave.network.ZWaveNetwork object at 0x7458e270>,
    # 'node': <openzwave.node.ZWaveNode object at 0x703c4fd0>} [node_queries_handler - zwave.py]
    # TODO: Mark devices as queries done
    # if str(node.node_id) in self._installed_nodes:
    #  command = Command(self._installed_nodes[str(node.node_id)], SERVICE_ID, 'ZWAVE_UPDATE', node=node, values=kwargs['value'], values_only=True)
    #  self._firefly.send_command(command)
    # else:
    #  logging.error('node %s not found in installed nodes' % str(node.node_id))

  def notification_handler(self, **kwargs):
    logging.message('ZWAVE NOTIFICATION HANDLER: %s' % str(kwargs))
    args = kwargs.get('args')
    if args is None:
      return
    node_id = args.get('nodeId')
    if str(node_id) in self._installed_nodes:
      node = self._network.nodes[node_id]
      command = Command(self._installed_nodes[str(node.node_id)], SERVICE_ID, 'ZWAVE_UPDATE', node=node)
      self._firefly.send_command(command)
    else:
      logging.error('node %s not found in installed nodes' % str(node_id))

  def zwave_refresh(self, **kwargs):
    if self._network.state >= 7 and not self.healed:
      try:
        if self._network.heal(True):
          self.healed = True
      except Exception as e:
        logging.error('ZWAVE HEAL ERROR: %s' % str(e))

    # Initial refresh of all nodes
    for node_id, node in self._network.nodes.items():
      node: ZWaveNode = node
      logging.error('ZWAVE DEBUG - node_id: %s' % str(node_id))
      logging.error('ZWAVE DEBUG - node: %s' % str(node))
      logging.message(str(node.get_sensors()))
      if len(node.get_sensors()) > 0:
        logging.message('ZWAVE SENSOR VALUE')
        logging.message(str(node.get_sensors()[list(node.get_sensors().keys())[0]].data))
      try:
        # node_id = node.node_id
        # if node.is_ready:
        logging.message('ZWAVE INFO: NODE %s IS READY: %s' % (str(node_id), str(node.is_ready)))
        command = Command(self._installed_nodes[str(node_id)], SERVICE_ID, 'ZWAVE_UPDATE', node=node)
        self._firefly.send_command(command)
        # else:
      except Exception as e:
        logging.error('ZWAVE ERROR: %s' % str(e))

  def stop(self):
    self.export()
    self._network.stop()

  def zwave_handler(self, *args, **kwargs):
    if kwargs.get('node') is None:
      return

    if type(kwargs.get('node')) is ZWaveController:
      return

    logging.debug('zwave change received %s' % kwargs.get('node').node_id)
    node_id = str(kwargs.get('node').node_id)
    if node_id not in self._installed_nodes:
      node = kwargs.get('node')
      try:
        self.add_child_nodes(node)
      except Exception as e:
        logging.error(code='FF.ZWA.ZWA.001', args=(node, e))  # error installing node %s: %s

    elif self._installed_nodes[node_id] not in self._firefly.components:
      self._installed_nodes.pop(node_id)
      node = kwargs.get('node')
      try:
        self.add_child_nodes(node)
      except:
        logging.error(code='FF.ZWA.ZWA.002', args=(node))  # error installing node %s


        # TODO: Change this to a send_command -> This will then do an update and broadcast
        # TODO: Pass all kwargs not just node

    else:
      node = kwargs.get('node')
      values = kwargs.get('value')
      logging.message('ZWAVE NODE IS READY: %s' % str(node.is_ready))
      logging.message('ZWAVE NETWORK STATUS: %s' % str(self._network.state_str))
      if not node.is_ready:
        logging.message('ZWAVE INFO: NODE %s IS NOT READY' % str(node_id))
        # try:
        #  command = Command(self._installed_nodes[node_id], SERVICE_ID, 'ZWAVE_UPDATE', node=node, values=values)
        #  self._firefly.send_command(command)
        # except Exception as e:
        #  logging.error(code='FF.ZWA.ZWA.003', args=(e))  # error sending command: %s
        # self._firefly.components[self._installed_nodes[node_id]].update_from_zwave(node, values=values)

  def add_child_nodes(self, node, **kwargs):
    node_id = str(node.node_id)
    product_name = node.product_name
    config = 'node.config'

    print('******************************************')
    print(node_id)
    print(node.to_dict())
    print(product_name)
    print(node.product_type)
    print(node.product_id)
    print(node.product_type)
    print(node.manufacturer_id)
    print(node.manufacturer_name)
    print('******************************************')

    alias = product_name
    if self.new_alias is not None:
      alias = self.new_alias

    if product_name in PACKAGE_MAPPING:
      package = 'Firefly.components.zwave.%s' % PACKAGE_MAPPING[product_name]
      device_id = self._firefly.install_package(package, alias=alias, node=node)
      self._installed_nodes[node_id] = device_id
      self.new_alias = None
      self.refresh_firebase()
      self.export()

    elif config in CONFIG_MAPPING:
      package = 'Firefly.components.zwave.%s' % CONFIG_MAPPING[config]
      device_id = self._firefly.install_package(package, alias=alias, node=node)
      self._installed_nodes[node_id] = device_id
      self.new_alias = None
      self.refresh_firebase()
      self.export()

    elif 'On/Off Power Switch' in node.device_type or 'On/Off Relay Switch' in node.device_type:
      device_id = self._firefly.install_package('Firefly.components.zwave.zwave_switch', alias=alias, node=node)
      self._installed_nodes[node_id] = device_id
      self.new_alias = None
      self.refresh_firebase()
      self.export()

    elif 'On/Off Power Switch' in product_name or 'On/Off Relay Switch' in product_name:
      device_id = self._firefly.install_package('Firefly.components.zwave.zwave_switch', alias=alias, node=node)
      self._installed_nodes[node_id] = device_id
      self.new_alias = None
      self.refresh_firebase()
      self.export()

    # TODO: update openzwave and see if this fixes this or create a new fix
    elif node.manufacturer_name == 'Leviton' and node.product_type == '0x1c02':
      device_id = self._firefly.install_package('Firefly.components.zwave.zwave_leviton_dzs15_switch', alias='Leviton Zwave Switch', node=node)
      self._installed_nodes[node_id] = device_id
      self.new_alias = None
      self.refresh_firebase()
      self.export()

    elif 'Door/Window Sensor' in product_name:
      device_id = self._firefly.install_package('Firefly.components.zwave.zwave_generic_devices.contact_sensor', alias=alias, node=node)
      self._installed_nodes[node_id] = device_id
      self.new_alias = None
      self.refresh_firebase()
      self.export()

    elif 'motion sensor' in product_name.lower() or 'motion detector' in product_name.lower():
      device_id = self._firefly.install_package('Firefly.components.zwave.zwave_generic_devices.motion_sensor', alias=alias, node=node)
      self._installed_nodes[node_id] = device_id
      self.new_alias = None
      self.refresh_firebase()
      self.export()

  def refresh_firebase(self):
    refresh_command = Command('service_firebase', 'zwave', 'refresh')
    self._firefly.send_command(refresh_command)

  def export(self):
    with open(ZWAVE_FILE, 'w') as f:
      json.dump({
        'installed_nodes': self._installed_nodes
      }, f, sort_keys=True, indent=4)

  def new_node(self, *args, **kwargs):
    logging.notify('New Node Added: %s' % kwargs)
    self.refresh_firebase()
    scheduler.cancel('zwave_cancel_pairing')
    pass

  def add_device(self, **kwargs):
    '''
    Hope we can add pairing mode
    Returns:

    '''
    self.new_alias = kwargs.get('alias')
    security = self._security_enable
    if kwargs.get('security'):
      security = True if kwargs.get('security') == 'true' else False
    # tries = 0
    # while self._network.controller.is_locked and tries < 30:
    #  logging.info('sleeping while zwave is locked. security_enabled=%s' % security)
    #  sleep(1)
    #  tries += 1
    # if tries >= 30:
    #  logging.notify('error unlocking zwave. Try again.')
    #  return
    logging.notify('Ready to pair zwave device.')
    logging.info('adding zwave: security: %s' % security)
    scheduler.runInM(3, self.cancel_command, job_id='zwave_cancel_pairing')
    return self._network.controller.add_node(doSecurity=security)

  def cancel_command(self):
    """
    Cancels the zwave command (This is how you would stop pairing mode etc..) Maybe this should get called when a new node
    notification is sent. This would make it so you can only add one device at a time.. Good thing?
    Returns:

    """
    self._network.controller.cancel_command()
    return True

  def remove_device(self):
    '''
    Hope we can add remove device mode
    Returns:

    '''
    self._network.controller.remove_node()

  def send_command(self):
    '''
    Function that zwave devices call when sending a command
    Returns:

    '''
    pass

  def get_update(self):
    '''
    This is the function that is called when updates for zwave are received. This function will
    then send updates to the child devices that updates were received for.
    Returns:

    '''
    pass

  def get_nodes(self):
    '''
    Get a list of nodes (nodeID, device serial, and device type)
    Returns:

    '''
    pass

  def get_orphans(self):
    '''
    Get a list of nodes that are orphaned and not in the alias file
    Returns:

    '''
    pass

  def poll_nodes(self, **kwargs):
    logging.info('polling zwave nodes')
    nodes = self._network.nodes.values()
    logging.debug('******************************** ZWAVE NODES ******************************')
    logging.debug(str(nodes))

    for n in nodes:
      # n:ZWaveNode = n
      n.request_state()
      sleep(1)
      n.refresh_info()
      sleep(10)
