ARG BASE_IMAGE=vyos/vyos-build:current
FROM ${BASE_IMAGE}

COPY syft.deb /tmp/syft.deb
COPY run-build.sh /usr/local/bin/vyos-ova-run-build
RUN sudo rm -f /usr/local/bin/syft \
    && sudo dpkg -i /tmp/syft.deb \
    && sudo rm -f /tmp/syft.deb \
    && sudo chmod 0755 /usr/local/bin/vyos-ova-run-build \
    && syft version
