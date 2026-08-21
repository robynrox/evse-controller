# src/evse_controller/mqtt.py
"""MQTT client for Wallbox state publishing and command handling."""

import paho.mqtt.client as mqtt_client
from evse_controller.utils.config import config
import ssl
import json
import queue
import logging
from evse_controller.event_bus import EventBus, EventType

logger = logging.getLogger('mqtt')

class MQTTManager:
    """Manages MQTT connection, publishing state, and receiving commands."""
    
    def __init__(self, execQueue: queue.SimpleQueue):
        self.execQueue = execQueue
        self._cached_inverter_data = {}
        self._setup_client()

    
    def _setup_client(self):
        """Configure and connect the MQTT client."""
        self.mqttclient = None
        if config.MQTT_BROKER == "":
            logger.info("MQTT not in use")
            return        
        self.mqttclient = mqtt_client.Client(config.MQTT_CLIENT_ID)
        self.mqttclient.on_connect = self._on_connect
        self.mqttclient.on_message = self._on_message
        if config.MQTT_USER and config.MQTT_PASS:
            self.mqttclient.username_pw_set(config.MQTT_USER, config.MQTT_PASS)
        if config.MQTT_TLS_ENABLED:
            try:
                self.mqttclient.tls_set(cert_reqs=ssl.CERT_REQUIRED,
                                tls_version=ssl.PROTOCOL_TLSv1_2)
            except Exception as e:
                logger.error(f"MQTT: Failed to configure TLS: {e}")
                return
        self.mqttclient.connect(config.MQTT_BROKER, config.MQTT_PORT)
        self.mqttclient.subscribe(f"{config.MQTT_CLIENT_ID}/request")
        self.mqttclient.loop_start()
        EventBus().subscribe(EventType.SYSTEM_STATE, self._on_system_state)
        EventBus().subscribe(EventType.INVERTER_STATE, self._on_inverter_state)

    
    def _on_connect(self, client, userdata, flags, rc):
        # Log the data rather than printing it
        if rc == 0:
            logger.info("Connected to MQTT Broker")
        else:
            logger.error(f"Failed to connect, return code {rc}")


    def _on_inverter_state(self, data):
        """Cache inverter data to include in the next system state update"""
        self._cached_inverter_data = data


    def _on_system_state(self, data):
        """Publish Wallbox state to wallbox/state topic.
        
        Args:
            wallbox_data: Dictionary containing state data.
        """
        if self.mqttclient:
            data_json = json.dumps(data | self._cached_inverter_data)
            logger.debug(f"{config.MQTT_CLIENT_ID}/state -> {data_json}")
            self.mqttclient.publish(f"{config.MQTT_CLIENT_ID}/state", data_json)
            self._cached_inverter_data = {}

    
    def _on_message(self, client, userdata, msg):
        """Handle incoming messages on command topics."""
        # Log the command rather than printing it
        logger.debug(f"{msg.topic} >- " + msg.payload.decode('utf-8'))
        # Parse it
        response = {}
        response["success"] = False
        correlation_id = None
        try:
            msg_json = json.loads(msg.payload)
            correlation_id = msg_json.get("id")
            command = msg_json.get("command")
            if isinstance(command, str) and command.strip():
                self.execQueue.put(command)
                response["success"] = True
                logger.info(f"Command '{command}' received from MQTT and queued")
            if correlation_id is not None:
                response["id"] = correlation_id
            response_json = json.dumps(response)
            logger.debug(f"{config.MQTT_CLIENT_ID}/response -> {response_json}")
            self.mqttclient.publish(f"{config.MQTT_CLIENT_ID}/response", response_json)
        except Exception as e:
            response["success"] = False
            response["error"] = f"General Exception: {e}"
            response["payload"] = msg.payload.decode('utf-8', errors='replace')
            if correlation_id is not None:
                response["id"] = correlation_id
            response_json = json.dumps(response)
            logger.exception("Error processing MQTT input message")
            logger.error(f"{config.MQTT_CLIENT_ID}/response -> {response_json}")
            self.mqttclient.publish(f"{config.MQTT_CLIENT_ID}/response", response_json)


    def shutdown(self):
        """Handle shutdown signals"""
        if self.mqttclient:
            self.mqttclient.loop_stop()
            self.mqttclient.disconnect()
            self.mqttclient = None
            logger.info("MQTT client shut down")
        else:
            logger.info("No MQTT client to shut down")
