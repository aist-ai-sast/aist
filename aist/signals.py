from django.dispatch import Signal

pipeline_status_changed = Signal()
pipeline_finished = Signal()
finding_deduplicated = Signal()
