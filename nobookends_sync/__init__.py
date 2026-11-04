"""NoBookEnds Sync: align audiobook narration to EPUB text."""

__version__ = "1.7.0"

# Schema version of the alignment JSON returned to clients (contract v1).
SCHEMA_VERSION = 1

# Bump when the alignment algorithm changes in a way that should trigger
# re-alignment of stored results. Transcripts are reused, so a re-run after a
# bump only repeats the (fast) alignment step.
ALGO_VERSION = 2
