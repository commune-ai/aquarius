#
# Copyright 2021 Ocean Protocol Foundation
# SPDX-License-Identifier: Apache-2.0
#
import json
import logging
import os
import time
from threading import Thread

import elasticsearch
from jsonsempai import magic  # noqa: F401

from aquarius.app.es_instance import ElasticsearchInstance
from aquarius.app.util import get_bool_env_value, get_allowed_publishers
from aquarius.block_utils import BlockProcessingClass
from aquarius.events.constants import EventTypes
from aquarius.events.processors import (
    MetadataCreatedProcessor,
    MetadataStateProcessor,
    MetadataUpdatedProcessor,
    OrderStartedProcessor,
    TokenURIUpdatedProcessor,
)
from aquarius.events.purgatory import Purgatory
from aquarius.events.util import (
    get_metadata_start_block,
    get_defined_block,
    get_fre,
    get_dispenser,
)
from artifacts import ERC20Template, ERC721Template
from web3.logs import DISCARD

logger = logging.getLogger(__name__)


class EventsMonitor(BlockProcessingClass):
    """Detect on-chain published Metadata and cache it in the database for
    fast retrieval and searchability.

    The published metadata is extracted from the `MetadataCreated`
    event log from the `Metadata` smartcontract. Metadata updates are also detected using
    the `MetadataUpdated` event.

    The Metadata json object is expected to be
    in an `lzma` compressed form and then encrypted. Decryption is done through Provider.

    The events monitor pauses for 25 seconds between updates.

    The cached Metadata can be restricted to only those published by specific ethereum accounts.
    To do this set the `ALLOWED_PUBLISHERS` envvar to the list of ethereum addresses of known publishers.



    """

    _instance = None

    def __init__(self, web3, config_file):
        self._es_instance = ElasticsearchInstance(config_file)

        self._other_db_index = f"{self._es_instance.db_index}_plus"
        self._es_instance.es.indices.create(index=self._other_db_index, ignore=400)

        self._web3 = web3

        self._chain_id = self._web3.eth.chain_id
        self.add_chain_id_to_chains_list()
        self._index_name = "events_last_block_" + str(self._chain_id)
        self._start_block = get_metadata_start_block()

        if get_bool_env_value("EVENTS_CLEAN_START", 0):
            self.reset_chain()

        self.get_or_set_last_block()
        self._allowed_publishers = get_allowed_publishers()
        logger.debug(f"allowed publishers: {self._allowed_publishers}")

        self._monitor_is_on = False
        default_sleep_time = 10
        try:
            self._monitor_sleep_time = int(
                os.getenv("OCN_EVENTS_MONITOR_QUITE_TIME", default_sleep_time)
            )
        except ValueError:
            self._monitor_sleep_time = default_sleep_time

        self._monitor_sleep_time = max(self._monitor_sleep_time, default_sleep_time)

        self.purgatory = (
            Purgatory(self._es_instance)
            if (os.getenv("ASSET_PURGATORY_URL") or os.getenv("ACCOUNT_PURGATORY_URL"))
            else None
        )

        purgatory_message = (
            "Enabling purgatory" if self.purgatory else "Purgatory is disabled"
        )
        logger.info("PURGATORY: " + purgatory_message)

    @property
    def block_envvar(self):
        return "METADATA_CONTRACT_BLOCK"

    def start_events_monitor(self):
        if self._monitor_is_on:
            return

        logger.info("Starting the events monitor.")
        t = Thread(target=self.run_monitor, daemon=True)
        self._monitor_is_on = True
        t.start()

    def stop_monitor(self):
        self._monitor_is_on = False

    def run_monitor(self):
        while True:
            self.do_run_monitor()
            time.sleep(self._monitor_sleep_time)

    def do_run_monitor(self):
        if not self._monitor_is_on:
            return

        try:
            self.process_current_blocks()
        except (KeyError, Exception) as e:
            logger.error(f"Error processing event: {str(e)}.")

        if self.purgatory:
            try:
                self.purgatory.update_lists()
            except (KeyError, Exception) as e:
                logger.error(f"Error updating purgatory list: {str(e)}.")

    def process_current_blocks(self):
        """Process all blocks from the last processed block to the current block."""
        last_block = self.get_last_processed_block()
        current_block = self._web3.eth.block_number
        if (
            not current_block
            or not isinstance(current_block, int)
            or current_block <= last_block
        ):
            return

        from_block = last_block

        start_block_chunk = from_block
        for end_block_chunk in range(
            from_block, current_block, self.blockchain_chunk_size
        ):
            self.process_block_range(start_block_chunk, end_block_chunk)
            start_block_chunk = end_block_chunk

        # Process last few blocks because range(start, end) doesn't include end
        self.process_block_range(end_block_chunk, current_block)

    def process_block_range(self, from_block, to_block):
        """Process a range of blocks."""
        logger.debug(
            f"Metadata monitor (chain: {self._chain_id})>>>> from_block:{from_block}, current_block:{to_block} <<<<"
        )

        if from_block > to_block:
            return

        processor_args = [
            self._es_instance,
            self._web3,
            self._allowed_publishers,
            self.purgatory,
            self._chain_id,
        ]

        event_processors = {
            "EVENT_METADATA_CREATED": MetadataCreatedProcessor,
            "EVENT_METADATA_UPDATED": MetadataUpdatedProcessor,
            "EVENT_METADATA_STATE": MetadataStateProcessor,
        }
        for event_name in event_processors:
            self.handle_regular_event_processor(
                event_name,
                event_processors[event_name],
                processor_args,
                from_block,
                to_block,
            )

        self.handle_price_change(from_block, to_block)
        self.handle_token_uri_update(from_block, to_block)

        self.store_last_processed_block(to_block)

    def handle_regular_event_processor(
        self, event_name, processor, processor_args, from_block, to_block
    ):
        """Process emitted events between two given blocks for a given event name.

        Args:
            event_name (str): event uppercase constant name
            processor (EventProcessor): event processor
            processor_args (List[any]): list of processors arguments
            from_block (int): inital block
            to_block (int): final block
        """
        for event in self.get_event_logs(
            EventTypes.get_value(event_name), from_block, to_block
        ):
            dt_contract = self._web3.eth.contract(
                abi=ERC721Template.abi,
                address=self._web3.toChecksumAddress(event.address),
            )
            receipt = self._web3.eth.get_transaction_receipt(
                event.transactionHash.hex()
            )
            event_object = dt_contract.events[
                EventTypes.get_value(event_name)
            ]().processReceipt(receipt, errors=DISCARD)[0]
            try:
                metadata_proofs = dt_contract.events.MetadataValidated().processReceipt(
                    receipt, errors=DISCARD
                )
                event_processor = processor(
                    *([event_object, dt_contract, receipt["from"]] + processor_args)
                )
                event_processor.metadata_proofs = metadata_proofs
                event_processor.process()
            except Exception as e:
                logger.exception(
                    f"Error processing {EventTypes.get_value(event_name)} event: {e}\n"
                    f"event={event}"
                )

    def handle_price_change(self, from_block, to_block):
        fre = get_fre(self._web3, self._chain_id)
        dispenser = get_dispenser(self._web3, self._chain_id)

        for event_name in [
            EventTypes.EVENT_ORDER_STARTED,
            EventTypes.EVENT_EXCHANGE_CREATED,
            EventTypes.EVENT_EXCHANGE_RATE_CHANGED,
            EventTypes.EVENT_DISPENSER_CREATED,
        ]:
            events = self.get_event_logs(event_name, from_block, to_block)

            for event in events:
                if event_name == EventTypes.EVENT_EXCHANGE_CREATED:
                    receipt = self._web3.eth.get_transaction_receipt(
                        event.transactionHash.hex()
                    )
                    erc20_address = receipt.to
                elif event_name == EventTypes.EVENT_EXCHANGE_RATE_CHANGED:
                    receipt = self._web3.eth.get_transaction_receipt(
                        event.transactionHash.hex()
                    )
                    exchange_id = (
                        fre.events.ExchangeRateChanged()
                        .processReceipt(receipt)[0]
                        .args.exchangeId
                    )
                    erc20_address = fre.caller.getExchange(exchange_id)[1]
                elif event_name == EventTypes.EVENT_DISPENSER_CREATED:
                    receipt = self._web3.eth.get_transaction_receipt(
                        event.transactionHash.hex()
                    )
                    erc20_address = (
                        dispenser.events.DispenserCreated()
                        .processReceipt(receipt)[0]
                        .args.datatokenAddress
                    )
                else:
                    erc20_address = event.address

                erc20_contract = self._web3.eth.contract(
                    abi=ERC20Template.abi,
                    address=self._web3.toChecksumAddress(erc20_address),
                )

                logger.debug(
                    f"{event_name} detected on ERC20 contract {event.address}."
                )

                try:
                    event_processor = OrderStartedProcessor(
                        erc20_contract.caller.getERC721Address(),
                        self._es_instance,
                        to_block,
                        self._chain_id,
                    )
                    event_processor.process()
                except Exception as e:
                    logger.error(
                        f"Error processing {event_name} event: {e}\n" f"event={event}"
                    )

    def handle_token_uri_update(self, from_block, to_block):
        events = self.get_event_logs(
            EventTypes.EVENT_TOKEN_URI_UPDATE, from_block, to_block
        )

        for event in events:
            try:
                event_processor = TokenURIUpdatedProcessor(
                    event, self._web3, self._es_instance, self._chain_id
                )
                event_processor.process()
            except Exception as e:
                logger.error(
                    f"Error processing token update event: {e}\n" f"event={event}"
                )

    def get_last_processed_block(self):
        block = get_defined_block(self._chain_id)
        try:
            # Re-establishing the connection with ES
            while True:
                try:
                    if self._es_instance.es.ping() is True:
                        break
                except elasticsearch.exceptions.ElasticsearchException as es_err:
                    logging.error(f"Elasticsearch error: {es_err}")
                logging.error("Connection to ES failed. Trying to connect to back...")
                time.sleep(5)
            logging.info("Stable connection to ES.")
            last_block_record = self._es_instance.es.get(
                index=self._other_db_index, id=self._index_name, doc_type="_doc"
            )["_source"]
            block = (
                last_block_record["last_block"]
                if last_block_record["last_block"] >= 0
                else get_defined_block(self._chain_id)
            )
        except Exception as e:
            # Retrieve the defined block.
            if type(e) == elasticsearch.NotFoundError:
                block = get_defined_block(self._chain_id)
                logger.info(f"Retrieved the default block. NotFound error occurred.")
            else:
                logging.error(f"Cannot get last_block error={e}")
        return block

    def store_last_processed_block(self, block):
        # make sure that we don't write a block < then needed
        stored_block = self.get_last_processed_block()
        if block <= stored_block:
            return
        record = {"last_block": block}
        try:
            self._es_instance.es.index(
                index=self._other_db_index,
                id=self._index_name,
                body=record,
                doc_type="_doc",
                refresh="wait_for",
            )["_id"]

        except elasticsearch.exceptions.RequestError:
            logger.error(
                f"store_last_processed_block: block={block} type={type(block)}, ES RequestError"
            )

    def add_chain_id_to_chains_list(self):
        try:
            chains = self._es_instance.es.get(
                index=self._other_db_index, id="chains", doc_type="_doc"
            )["_source"]
        except Exception:
            chains = dict()
        chains[str(self._chain_id)] = True

        try:
            self._es_instance.es.index(
                index=self._other_db_index,
                id="chains",
                body=json.dumps(chains),
                doc_type="_doc",
                refresh="wait_for",
            )["_id"]
            logger.info(f"Added {self._chain_id} to chains list")
        except elasticsearch.exceptions.RequestError:
            logger.error(
                f"Cannot add chain_id {self._chain_id} to chains list: ES RequestError"
            )

    def reset_chain(self):
        assets = self.get_assets_in_chain()
        for asset in assets:
            try:
                self._es_instance.delete(asset["id"])
            except Exception as e:
                logging.error(f"Delete asset failed: {str(e)}")

        self.store_last_processed_block(self._start_block)

    def get_assets_in_chain(self):
        body = {
            "query": {
                "query_string": {"query": self._chain_id, "default_field": "chainId"}
            }
        }
        page = self._es_instance.es.search(index=self._es_instance.db_index, body=body)
        total = page["hits"]["total"]["value"]
        body["size"] = total
        page = self._es_instance.es.search(index=self._es_instance.db_index, body=body)

        object_list = []
        for x in page["hits"]["hits"]:
            object_list.append(x["_source"])

        return object_list

    def get_event_logs(self, event_name, from_block, to_block, chunk_size=1000):
        if event_name not in EventTypes.get_all_values():
            return []

        if event_name == EventTypes.EVENT_METADATA_CREATED:
            hash_text = "MetadataCreated(address,uint8,string,bytes,bytes,bytes32,uint256,uint256)"
        elif event_name == EventTypes.EVENT_METADATA_UPDATED:
            hash_text = "MetadataUpdated(address,uint8,string,bytes,bytes,bytes32,uint256,uint256)"
        elif event_name == EventTypes.EVENT_METADATA_STATE:
            hash_text = "MetadataState(address,uint8,uint256,uint256)"
        elif event_name == EventTypes.EVENT_TOKEN_URI_UPDATE:
            hash_text = "TokenURIUpdate(address,string,uint256,uint256,uint256)"
        elif event_name == EventTypes.EVENT_EXCHANGE_CREATED:
            hash_text = "ExchangeCreated(bytes32,address,address,address,uint256)"
        elif event_name == EventTypes.EVENT_EXCHANGE_RATE_CHANGED:
            hash_text = "ExchangeRateChanged(bytes32,address,uint256)"
        elif event_name == EventTypes.EVENT_DISPENSER_CREATED:
            hash_text = "DispenserCreated(address,address,uint256,uint256,address)"
        else:
            hash_text = (
                "OrderStarted(address,address,uint256,uint256,uint256,address,uint256)"
            )

        event_signature_hash = self._web3.keccak(text=hash_text).hex()

        _from = from_block
        _to = min(_from + chunk_size - 1, to_block)

        logger.info(
            f"Searching for {event_name} events on chain {self._chain_id} "
            f"in blocks {from_block} to {to_block}."
        )

        filter_params = {
            "topics": [event_signature_hash],
            "fromBlock": _from,
            "toBlock": _to,
        }

        all_logs = []
        while _from <= to_block:
            # Search current chunk
            logs = self._web3.eth.get_logs(filter_params)
            all_logs.extend(logs)
            if (_from - from_block) % 1000 == 0:
                logger.debug(
                    f"Searched blocks {_from} to {_to} on chain {self._chain_id}"
                    f"{len(all_logs)} {event_name} events detected so far."
                )

            # Prepare for next chunk
            _from = _to + 1
            _to = min(_from + chunk_size - 1, to_block)
            filter_params.update({"fromBlock": _from, "toBlock": _to})

        logger.info(
            f"Finished searching for {event_name} events on chain {self._chain_id} "
            f"in blocks {from_block} to {to_block}. "
            f"{len(all_logs)} {event_name} events detected."
        )

        return all_logs
