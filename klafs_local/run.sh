#!/usr/bin/with-contenv bashio
# Options + MQTT credentials from the Supervisor into one JSON file (only inside the container, never logged)
OPT=/data/options.json
RUN=/tmp/klafs_options.json
MODULE_IP=$(bashio::config 'module_ip')
ROUTER_IP=$(bashio::config 'router_ip')
# Return route for a DNAT setup (router rewrites the cloud address to this host): answers must go back via the router
if [ -n "$MODULE_IP" ] && [ -n "$ROUTER_IP" ]; then
  ip route replace "$MODULE_IP/32" via "$ROUTER_IP" && bashio::log.info "route $MODULE_IP via $ROUTER_IP"
fi
LOGDIR=""
if bashio::config.true 'log_frames'; then LOGDIR=/share/klafs_local; fi
jq --arg h "$(bashio::services mqtt host)" --arg p "$(bashio::services mqtt port)" \
   --arg u "$(bashio::services mqtt username)" --arg pw "$(bashio::services mqtt password)" \
   --arg tz "$(bashio::info.timezone)" --arg ld "$LOGDIR" \
   '. + {mqtt_host:$h, mqtt_port:($p|tonumber), mqtt_user:$u, mqtt_password:$pw, timezone:$tz, log_dir:$ld}' "$OPT" > "$RUN"
chmod 600 "$RUN"
exec python3 /klafs_local.py --options "$RUN"
