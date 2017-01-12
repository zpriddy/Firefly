import asyncio
from typing import Any

from aiohttp import web

from Firefly.helpers.events import (Event, Command, Request)

from Firefly.helpers.subscribers import Subscriptions
from Firefly.helpers.alias import Alias
from Firefly import logging
from Firefly import aliases
from Firefly import scheduler
from Firefly.helpers.location import Location
from Firefly.const import (ACTION_ON)

import importlib

import os

app = web.Application()



class Firefly(object):
  ''' Core running loop and scheduler of Firefly'''
  def __init__(self, settings):
    logging.message('Initializing Firefly')
    self.settings = settings
    self.loop = asyncio.get_event_loop()
    self.subscriptions = Subscriptions()
    self.location = Location(self, "95110", ['HOME'])

    # TODO: Expand this
    self._devices = {}
    here = os.path.join(os.path.split(__file__)[0])
    print(here)
    module = importlib.import_module('Firefly.devices.test_device')
    package = module.Setup(self, alias='Test Device')
    #module = importlib.import_module('firefly.devices.test_device', package)
    #new_device = module.TestDevice()
    #self._devices['test_device'] = new_device

    # TODO: Remove this. This is a POC for scheduler.
    c = Command('Test Device', 'web_api', ACTION_ON)
    print(c.export())
    d_args = c.export()
    d = Command(**d_args)
    scheduler.runEveryS(15, self.send_command, command=d)


    # TODO: Leave In.
    scheduler.runEveryM(10, self.export_current_values)



  def start(self) -> None:
    ''' Start up Firefly.
    '''
    # TODO: Import current state of devices on boot.

    logging.message('Starting Firefly')

    try:
      web.run_app(app, host=self.settings.firefly_host, port=self.settings.firefly_port)
    except KeyboardInterrupt:
      pass
    finally:
      self.stop()


  def stop(self) -> None:
    ''' Shutdown firefly.

    Shutdown process should export the current state of all devices so it can be imported on reboot and startup.
    '''
    # TODO: Export current state of devices on shutdown

    logging.message('Stopping Firefly')
    self.export_current_values()

    # TODO: Remove this once exporting works
    for device in self.devices:
      print(self.devices[device].__dict__)

    self.loop.close()

  def add_task(self, task):
    logging.debug('Adding task to Firefly scheduler: %s' % str(task))
    future = asyncio.Future()
    r = yield from asyncio.ensure_future(task)
    future.set_result(r)
    return r

  def export_current_values(self) -> None:
    """
    Export current values to backup files to restore current config on reboot.
    """
    logging.message('Exporting current config.')
    aliases.export_aliases()

  @asyncio.coroutine
  def send_event(self, event: Event) -> None:
    yield from self.add_task(self._send_event(event))

  @asyncio.coroutine
  def _send_event(self, event: Event) -> None:
    send_to = self.subscriptions.get_subscribers(event.source, event_action=event.event_action)
    logging.debug('Sending Event: %s -> %s' % (event, str(send_to)))
    for s in send_to:
      yield from self.__send_event(s, event)

  @asyncio.coroutine
  def __send_event(self, send_to: str, event: Event) -> None:
    self.devices[send_to].event(event)

  @asyncio.coroutine
  def send_request(self, request: Request) -> Any:
    """
    Send a request to a device or app.

    Args:
      request (Request): Request to be sent

    Returns:
      Requested data.
    """
    result = yield from self.add_task(self._send_request(request))
    return result

  @asyncio.coroutine
  def _send_request(self, request: Request) -> Any:
    if request.device not in self.devices.keys():
      return False
    return self.devices[request.device].request(request)


  @asyncio.coroutine
  def send_command(self, command: Command) -> bool:
    """
    Send a command to a device or app.

    Args:
      command (Command):

    Returns:
      (bool): Command successfully sent.
    """
    result = yield from self.add_task(self._send_command(command))
    return result

  @asyncio.coroutine
  def _send_command(self, command: Command) -> bool:
    if command.device not in self.devices.keys():
      return False
    return self.devices[command.device].command(command)


  def add_route(self, route, method, handler):
    app.router.add_route(method, route, handler)

  def add_get(self, route, handler, *args):
    app.router.add_get(route, handler)


  @property
  def devices(self):
    return self._devices


async def myTestFunction():
  print('Running Test Function')