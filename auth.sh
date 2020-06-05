#!/bin/bash

SITE="ams"

openconnect-sso \
    --version-string 4.7.00136 \
    -g ssl \
    --headless \
    --server https://${SITE}-vpn-cluster.company.com

