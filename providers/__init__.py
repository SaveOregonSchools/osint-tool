"""Provider clients for Social OSINT query plugins.

Provider modules keep API-specific request and pagination details out of the
Flask plugin files. They should not read or persist secrets beyond the current
request.
"""
