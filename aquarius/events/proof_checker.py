import json
from json import JSONDecodeError
import logging
import os

logger = logging.getLogger(__name__)


def check_metadata_proofs(web3, metadata_proofs):
    allowed_validators = os.getenv("ALLOWED_VALIDATORS")

    if not allowed_validators:
        return True

    try:
        allowed_validators = set(json.loads(allowed_validators))
    except (JSONDecodeError, TypeError, Exception) as e:
        logger.error(f"Reading list of allowed validators failed: {e}.")

        return False

    if not allowed_validators:
        return True

    try:
        proof_addresses = {
            web3.toChecksumAddress(metadata_proof.args.validator)
            for metadata_proof in metadata_proofs
        }

        allowed_addresses = {
            web3.toChecksumAddress(address) for address in allowed_validators
        }

        allowed_addresses = list(proof_addresses & allowed_addresses)

        return bool(allowed_addresses)
    except Exception:
        logger.info("Exception retrieving proof addresses or validation proofs.")
        return False
