ARG BASE_IMAGE=vyos/vyos-build:current
FROM ${BASE_IMAGE}

COPY syft.deb /tmp/syft.deb
RUN sudo rm -f /usr/local/bin/syft \
    && sudo dpkg -i /tmp/syft.deb \
    && sudo rm -f /tmp/syft.deb \
    && syft version
