"""Clean Media Worker — self-hosted AI media analysis for Jellyfin."""

# Project version. The worker and the Jellyfin plugin are released as a matched
# pair from this one repo: a git tag `vX.Y.Z` contains this worker and plugin
# X.Y.Z.0. The release workflow stamps this string in lockstep with the plugin.
__version__ = "0.2.10"

# HTTP contract version between the plugin and this worker. Unlike __version__
# (which bumps every release), this integer bumps ONLY when the /api surface
# changes in a way that would break an older plugin. The plugin compares it
# against the contract it was built for and shows a compatible / update-needed
# banner on its settings page — so a slightly-behind plugin still works, and a
# genuine mismatch is reported instead of silently misbehaving.
API_VERSION = 1
