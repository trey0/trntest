# jupyter-server-proxy config (loaded via `jupyter lab --config=<this file>`, see docker/Dockerfile's
# CMD). Proxies /output/... on this same Jupyter server/port to a plain `python3 -m http.server`
# rooted at /workspace/output -- gives every dataset under output/ (not just one, unlike
# scripts/serve_reports.sh) real directory-listing ("autoindex") support and, as a side effect, none
# of Jupyter's own AuthenticatedFileHandler restrictions (sandboxed opaque origin,
# frame-ancestors 'self'), since jupyter-server-proxy forwards the backend's own response headers
# instead of Jupyter's default ones -- see docs/report-generation.md's "Viewing reports" section for
# what this newly makes possible (including the reports nav bar's iframe, previously unfixable
# through JupyterLab's own server no matter what).
#
# Plain http.server, not a custom wrapper: `TrnTestEntry.log_path` deliberately names its captured
# generator logs `<product_type>_log.txt`, not `<product_type>.log` -- `.txt` is a real
# stdlib-`mimetypes`-recognized extension, so http.server already serves it as `text/plain` (a
# browser displays this inline) without needing any extension-registration workaround. See that
# property's own docstring for why `.log` alone doesn't work here (`application/octet-stream`,
# which browsers download instead of displaying).
#
# The backend process (plain stdlib http.server, no auth/CSP of its own) is started lazily, on first
# request, and supervised/torn down by jupyter-server-proxy itself -- no separate script or manual
# lifecycle management needed, unlike scripts/serve_reports.sh's own separate `docker compose run`
# invocation.
c.ServerProxy.servers = {  # noqa: F821 -- `c` is injected by Jupyter's own config-file loader
    "output": {
        "command": ["python3", "-m", "http.server", "{port}", "--directory", "/workspace/output"],
        "absolute_url": False,
        "launcher_entry": {"enabled": False},  # a plain file browser, not an interactive app --
        # no launcher tile needed; reach it via a direct /output/... URL instead.
    }
}
