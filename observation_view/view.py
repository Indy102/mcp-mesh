"""One public D2 read, followed by exclusive private snapshot publication."""
import os
from pathlib import Path
import re
import uuid

from adapters.health_observations import contracts as d1
import observation_store
from observation_store import filesystem as fs
from observation_store.contracts import uuid4
from observation_store.errors import StoreError
from .errors import ViewError, acknowledgement
from .rendering import _render


def _destination(output, store_dir):
    try:
        requested = Path(output).absolute()
        if os.path.lexists(requested):
            raise ViewError('OUTPUT_EXISTS')
        resolved = fs.reject_location(requested)
        store = Path(store_dir).resolve()
        if resolved == store or store in resolved.parents:
            raise ViewError('OUTPUT_INVALID')
        for parent in resolved.parents:
            if os.path.lexists(parent / 'manifest.json') and os.path.lexists(parent / 'writer.lock'):
                raise ViewError('OUTPUT_INVALID')
        return resolved
    except (TypeError, ValueError, OSError):
        raise ViewError('OUTPUT_INVALID') from None


def create_view(store_dir, store_id, batch_id, output):
    """Validate an explicit committed batch and publish a new private HTML file.

    No store writes or fresh source replay. A successful read validates linked
    evidence at generation time; opening the resulting file does not revalidate.
    Only fixed acknowledgements/errors leave this API; the HTML stays private.
    """
    publication_started = False
    try:
        try:
            uuid4(store_id)
            if type(batch_id) is not str or re.fullmatch('[0-9a-f]{64}', batch_id) is None:
                raise ValueError()
            if not isinstance(store_dir, (str, os.PathLike)):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ViewError('INPUT_INVALID') from None
        fs.require_platform()
        target = _destination(output, store_dir)
        try:
            parent = fs.CheckedDir.open(target.parent)
        except StoreError:
            raise ViewError('OUTPUT_NOT_PRIVATE') from None
        with parent:
            raw = observation_store.read_batch(store_dir, store_id, batch_id)
            try:
                batch = d1.validate_batch(d1.strict_load(raw))
                if batch['batch_id'] != batch_id:
                    raise ValueError()
            except Exception:
                raise ViewError('INTEGRITY_ERROR') from None
            html = _render(batch, store_id)
            # Recheck exclusions after the potentially long archive validation.
            if _destination(output, store_dir) != target:
                raise ViewError('OUTPUT_INVALID')
            parent.check()
            stage = '.health-view-' + str(uuid.uuid4())
            fs.stage_bytes(parent, stage, html)
            publication_started = True
            try:
                fs.publish(parent, stage, parent, target.name)
            except fs.PublicationError as error:
                if error.linked_may_have_occurred:
                    raise ViewError('OUTPUT_PUBLICATION_UNKNOWN') from None
                publication_started = False
                raise ViewError(error.code) from None
        return acknowledgement()
    except ViewError:
        raise
    except StoreError as error:
        raise ViewError('OUTPUT_PUBLICATION_UNKNOWN' if publication_started else error.code) from None
    except Exception:
        raise ViewError('OUTPUT_PUBLICATION_UNKNOWN' if publication_started else 'INTERNAL_ERROR') from None
