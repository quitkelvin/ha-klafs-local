ARG BUILD_FROM
FROM $BUILD_FROM
RUN apk add --no-cache python3 py3-pip tzdata iproute2 \
 && pip3 install --no-cache-dir --break-system-packages paho-mqtt==2.1.0
COPY klafs_local.py run.sh /
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
