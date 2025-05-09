#!/usr/bin/env python3

"""
 Web Socket Gateway

 Copyright (c) 2019-2024 Alan Yorinks All right reserved.

 Python Banyan is free software; you can redistribute it and/or
 modify it under the terms of the GNU AFFERO GENERAL PUBLIC LICENSE
 Version 3 as published by the Free Software Foundation; either
 or (at your option) any later version.
 This library is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 General Public License for more details.

 You should have received a copy of the GNU AFFERO GENERAL PUBLIC LICENSE
 along with this library; if not, write to the Free Software
 Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""

import argparse
import asyncio
import datetime
import json
import logging
import pathlib
import signal
import sys
import websockets

from python_banyan.banyan_base_aio import BanyanBaseAIO


class WsGateway(BanyanBaseAIO):
    """
    This class is a gateway between a websocket client and the
    Banyan network.

    NOTE: This class requires Python 3.7 or above.

    It implements a websocket server. A websocket client, upon
    connection, must send an id message e.g.: {"id": "to_arduino"}.

    The id will be used as the topic to publish data to the banyan
    network.
    """

    def __init__(self, subscription_list, back_plane_ip_address=None,
                 subscriber_port='43125',
                 publisher_port='43124', process_name='WebSocketGateway',
                 event_loop=None, server_ip_port=9000, log=False):
        """
        These are all the normal base class parameters
        :param subscription_list:
        :param back_plane_ip_address:
        :param subscriber_port:
        :param publisher_port:
        :param process_name:
        :param event_loop:
        """

        # set up logging if requested
        self.log = log
        self.event_loop = event_loop

        # a kludge to shutdown the socket on control C
        self.wsocket = None

        if self.log:
            fn = str(pathlib.Path.home()) + "/wsgw.log"
            self.logger = logging.getLogger(__name__)
            logging.basicConfig(filename=fn, filemode='w', level=logging.DEBUG)
            sys.excepthook = self.my_handler

        # initialize the base class
        super(WsGateway, self).__init__(subscriber_list=subscription_list,
                                        back_plane_ip_address=back_plane_ip_address,
                                        subscriber_port=subscriber_port,
                                        publisher_port=publisher_port,
                                        process_name=process_name,
                                        event_loop=self.event_loop)

        # save the server port number
        self.server_ip_port = server_ip_port

        # array of active sockets
        self.active_sockets = []
        try:
            self.start_server = websockets.serve(self.wsg,
                                                 '0.0.0.0',
                                                 self.server_ip_port)
            print('WebSocket using: ' + self.back_plane_ip_address
                  + ':' + self.server_ip_port)
            # start the websocket server and call the main task, wsg
            self.event_loop.run_until_complete(self.start_server)
            self.event_loop.create_task(self.wakeup())
            self.event_loop.run_forever()
        except (websockets.exceptions.ConnectionClosed,
                RuntimeError,
                KeyboardInterrupt):
            if self.log:
                logging.exception("Exception occurred", exc_info=True)
            self.event_loop.stop()
            self.event_loop.close()

    async def wakeup(self):
        while True:
            try:
                await asyncio.sleep(1)
            except KeyboardInterrupt:
                for task in asyncio.Task.all_tasks():
                    task.cancel()
                await self.wsocket.close()
                self.event_loop.stop()
                self.event_loop.close()
                sys.exit(0)

    async def wsg(self, websocket, path):
        """
        This method handles connections and will be used to send
        messages to the client
        :param websocket: websocket for connected client
        :param path: required, but unused
        :return:
        """
        self.wsocket = websocket
        # start up banyan
        await self.begin()

        # wait for a connection
        try:
            data = await websocket.recv()
        except websockets.exceptions.ConnectionClosedOK:
            pass

        # expecting an id string from client
        data = json.loads(data)

        # if id field not present then raise an exception
        try:
            id_string = data['id']
        except KeyError:
            print('Client did not provide an ID string')
            raise

        # create a subscriber string from the id
        subscriber_string = id_string.replace('to', 'from')

        # subscribe to that topic
        await self.set_subscriber_topic(subscriber_string)

        # add an entry into the active_sockets table
        entry = {websocket: 'to_banyan_topic', subscriber_string: websocket}
        self.active_sockets.append(entry)

        # create a task to receive messages from the client
        await asyncio.create_task(self.receive_data(websocket, data['id']))

    async def receive_data(self, websocket, publisher_topic):
        """
        This method processes a received WebSocket command message
        and translates it to a Banyan command message.
        :param websocket: The currently active websocket
        :param publisher_topic: The publishing topic
        """
        while True:
            try:
                data = await websocket.recv()
                data = json.loads(data)
            except (websockets.exceptions.ConnectionClosed, TypeError):
                # remove the entry from active_sockets
                # using a list comprehension
                self.active_sockets = [entry for entry in self.active_sockets if
                                       websocket not in entry]
                break

            await self.publish_payload(data, publisher_topic)

    async def incoming_message_processing(self, topic, payload):
        """
        This method converts the incoming messages to ws messages
        and sends them to the ws client

        :param topic: Message Topic string.

        :param payload: Message Data.
        """
        if payload['report'] == 'panic':
            # close the sockets if in panic mode
            for socket in self.active_sockets:
                if topic in socket.keys():
                    pub_socket = socket[topic]
                    await pub_socket.close()

        if 'timestamp' in payload:
            timestamp = datetime.datetime.fromtimestamp(payload['timestamp']).strftime(
                '%Y-%m-%d %H:%M:%S')
            payload['timestamp'] = timestamp

        ws_data = json.dumps(payload)

        # find the websocket of interest by looking for the topic in
        # active_sockets
        for socket in self.active_sockets:
            if topic in socket.keys():
                pub_socket = socket[topic]
                await pub_socket.send(ws_data)

    def my_handler(self, the_type, value, tb):
        """
        For logging uncaught exceptions
        :param the_type:
        :param value:
        :param tb:
        :return:
        """
        self.logger.exception("Uncaught exception: {0}".format(str(value)))


def ws_gateway():
    # allow user to bypass the IP address auto-discovery. This is necessary if the component resides on a computer
    # other than the computing running the backplane.

    parser = argparse.ArgumentParser()
    parser.add_argument("-b", dest="back_plane_ip_address", default="None",
                        help="None or IP address used by Back Plane")
    # allow the user to specify a name for the component and have it shown on the console banner.
    # modify the default process name to one you wish to see on the banner.
    # change the default in the derived class to set the name
    parser.add_argument("-m", dest="subscription_list", default="from_arduino_gateway, "
                                                                "from_esp8266_gateway, "
                                                                "from_rpi_gateway, "
                                                                "from_microbit_gateway"
                                                                "from_picoboard_gateway"
                                                                "from_cpx_gateway",
                        nargs='+',
                        help="A space delimited list of topics")
    parser.add_argument("-i", dest="server_ip_port", default="9000",
                        help="Set the WebSocket Server IP Port number")
    parser.add_argument("-l", dest="log", default="False",
                        help="Set to True to turn logging on.")
    parser.add_argument("-n", dest="process_name", default="WebSocket Gateway",
                        help="Set process name in banner")
    parser.add_argument("-p", dest="publisher_port", default='43124',
                        help="Publisher IP port")
    parser.add_argument("-s", dest="subscriber_port", default='43125',
                        help="Subscriber IP port")

    args = parser.parse_args()

    subscription_list = args.subscription_list

    if len(subscription_list) > 1:
        subscription_list = args.subscription_list.split(',')

    kw_options = {
        'publisher_port': args.publisher_port,
        'subscriber_port': args.subscriber_port,
        'process_name': args.process_name,
        'server_ip_port': args.server_ip_port,
    }

    log = args.log.lower()
    if log == 'false':
        log = False
    else:
        log = True

    if args.back_plane_ip_address != 'None':
        kw_options['back_plane_ip_address'] = args.back_plane_ip_address

    # get the event loop
    # this is for python 3.8
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        WsGateway(subscription_list, **kw_options, event_loop=loop)
    except KeyboardInterrupt:
        for task in asyncio.Task.all_tasks():
            task.cancel()
        loop.stop()
        loop.close()
        sys.exit(0)


def signal_handler(sig, frame):
    print('Exiting Through Signal Handler')
    raise KeyboardInterrupt


# listen for SIGINT
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == '__main__':
    ws_gateway()
