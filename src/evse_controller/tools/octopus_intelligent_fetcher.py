#!/usr/bin/env python3
"""
Simple script to fetch Octopus Intelligent tariff information using the GraphQL API.

Usage:
    python octopus_intelligent_fetcher.py --api-key <your_api_key> --account-id <your_account_id>

Or using environment variables:
    export OCTOPUS_API_KEY="your_api_key"
    export OCTOPUS_ACCOUNT_ID="your_account_id"
    python octopus_intelligent_fetcher.py
"""

import argparse
import asyncio
import os
import sys
import json
from datetime import datetime
from gql import Client, gql
from gql.transport.aiohttp import AIOHTTPTransport


class OctopusIntelligentFetcher:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("API KEY is not set")
        
        self._api_key = api_key
        self._base_url = "https://api.octopus.energy/v1/graphql/"
        self._session = None

    async def _get_token(self):
        """Gets a new token from the API"""
        transport = AIOHTTPTransport(url=self._base_url)

        async with Client(
            transport=transport,
            fetch_schema_from_transport=True,
        ) as session:
            query = gql(
                '''
                mutation krakenTokenAuthentication($apiKey: String!) {
                  obtainKrakenToken(input: { APIKey: $apiKey })
                  {
                    token
                  }
                }
                '''
            )

            params = {"apiKey": self._api_key}
            result = await session.execute(
                query, 
                variable_values=params, 
                operation_name="krakenTokenAuthentication"
            )
            return result['obtainKrakenToken']['token']

    async def _get_session(self):
        """Creates a new authenticated session"""
        if self._session:
            return self._session

        token = await self._get_token()
        headers = {"Authorization": token}
        transport = AIOHTTPTransport(url=self._base_url, headers=headers)

        session = Client(
            transport=transport,
            fetch_schema_from_transport=True,
        )
        self._session = session
        return session

    async def get_combined_state(self, account_id: str):
        """Get the user's account state - vehicle preferences, device info, and planned dispatches"""
        async with await self._get_session() as session:
            query = gql(
                '''
                    query getCombinedData($accountNumber: String!) {
                        vehicleChargingPreferences(accountNumber: $accountNumber) {
                            weekdayTargetTime,
                            weekdayTargetSoc,
                            weekendTargetTime,
                            weekendTargetSoc
                        }
                        registeredKrakenflexDevice(accountNumber: $accountNumber) {
                            krakenflexDeviceId
                            provider
                            vehicleMake
                            vehicleModel
                            vehicleBatterySizeInKwh
                            chargePointMake
                            chargePointModel
                            chargePointPowerInKw
                            status
                            suspended
                            hasToken
                            createdAt
                        }
                        plannedDispatches(accountNumber: $accountNumber) {
                            startDtUtc: startDt
                            endDtUtc: endDt
                            chargeKwh: delta
                            meta { 
                                source
                                location
                            }
                        }
                        completedDispatches(accountNumber: $accountNumber) {
                            startDtUtc: startDt
                            endDtUtc: endDt
                            chargeKwh: delta
                            meta { 
                                source
                                location
                            }
                        }
                    }
                '''
            )

            params = {"accountNumber": account_id}
            result = await session.execute(query, variable_values=params, operation_name="getCombinedData")
            return result

    async def get_device_info(self, account_id: str):
        """Get device info only"""
        async with await self._get_session() as session:
            query = gql(
                '''
                  query registeredKrakenflexDevice($accountNumber: String!) {
                    registeredKrakenflexDevice(accountNumber: $accountNumber) {
                      krakenflexDeviceId
                      provider
                      vehicleMake
                      vehicleModel
                      vehicleBatterySizeInKwh
                      chargePointMake
                      chargePointModel
                      chargePointPowerInKw
                      status
                      suspended
                      hasToken
                      createdAt
                    }
                  }
                '''
            )

            params = {"accountNumber": account_id}
            result = await session.execute(query, variable_values=params, operation_name="registeredKrakenflexDevice")
            return result['registeredKrakenflexDevice']

    async def get_planned_dispatches(self, account_id: str):
        """Get planned dispatches only"""
        async with await self._get_session() as session:
            query = gql(
                '''
                  query plannedDispatches($accountNumber: String!) {
                    plannedDispatches(accountNumber: $accountNumber) {
                        startDtUtc: startDt
                        endDtUtc: endDt
                        chargeKwh: delta
                        meta { 
                            source
                            location
                        }
                    }
                  }
                '''
            )

            params = {"accountNumber": account_id}
            result = await session.execute(query, variable_values=params, operation_name="plannedDispatches")
            return result['plannedDispatches']


async def main():
    parser = argparse.ArgumentParser(description='Fetch Octopus Intelligent Tariff Information')
    parser.add_argument('--api-key', help='Octopus API Key (can also be set via OCTOPUS_API_KEY environment variable)')
    parser.add_argument('--account-id', help='Octopus Account ID (can also be set via OCTOPUS_ACCOUNT_ID environment variable)')
    parser.add_argument('--device-only', action='store_true', help='Fetch only device info')
    parser.add_argument('--dispatches-only', action='store_true', help='Fetch only planned dispatches')
    
    args = parser.parse_args()

    # Get credentials from command line or environment variables
    api_key = args.api_key or os.getenv('OCTOPUS_API_KEY')
    account_id = args.account_id or os.getenv('OCTOPUS_ACCOUNT_ID')

    if not api_key:
        print("Error: API key not provided. Use --api-key or set OCTOPUS_API_KEY environment variable.", file=sys.stderr)
        sys.exit(1)
    
    if not account_id:
        print("Error: Account ID not provided. Use --account-id or set OCTOPUS_ACCOUNT_ID environment variable.", file=sys.stderr)
        sys.exit(1)

    try:
        fetcher = OctopusIntelligentFetcher(api_key)
        
        if args.device_only:
            device_info = await fetcher.get_device_info(account_id)
            print(json.dumps(device_info, indent=2))
        elif args.dispatches_only:
            dispatches = await fetcher.get_planned_dispatches(account_id)
            print(json.dumps(dispatches, indent=2))
        else:
            data = await fetcher.get_combined_state(account_id)
            print(json.dumps(data, indent=2))
    
    except Exception as e:
        print(f"Error fetching data: {str(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())