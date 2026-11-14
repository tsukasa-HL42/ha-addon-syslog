#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

bashio::log.info "Set configuration..."
SYSLOG_HOST=$(bashio::config 'syslog_host')
export SYSLOG_HOST
SYSLOG_PORT=$(bashio::config 'syslog_port')
export SYSLOG_PORT
SYSLOG_PROTO=$(bashio::config 'syslog_protocol')
export SYSLOG_PROTO
SYSLOG_SSL=$(bashio::config 'syslog_ssl')
export SYSLOG_SSL
SYSLOG_SSL_VERIFY=$(bashio::config 'syslog_ssl_verify')
export SYSLOG_SSL_VERIFY
SYSLOG_FORMAT=$(bashio::config 'syslog_format')
export SYSLOG_FORMAT
# one pattern per line
REDACT_PATTERNS=$(bashio::config 'redact_patterns')
export REDACT_PATTERNS
# one pattern per line, matched against "app: message"
EXCLUDE_PATTERNS=$(bashio::config 'exclude_patterns')
export EXCLUDE_PATTERNS
BACKLOG_MAX_HOURS=$(bashio::config 'backlog_max_hours' 6)
export BACKLOG_MAX_HOURS
MAX_MESSAGE_BYTES=$(bashio::config 'max_message_bytes' 8192)
export MAX_MESSAGE_BYTES
# persistent app storage: the journal position survives restarts
export STATE_DIR=/data
HAOS_HOSTNAME=$(bashio::info.hostname)
export HAOS_HOSTNAME

# Run daemon; exec so that it receives SIGTERM and saves its position on stop
bashio::log.info "Starting the daemon..."
exec python3 /journal2syslog.py
