import re

with open("analytics_platform/api.py", "r") as f:
    content = f.read()

middleware_code = """
    @app.middleware("http")
    async def legacy_tenant_rewriter(request: Request, call_next):
        path = request.scope.get("path", "")
        # The frontend hardcodes tenant ID '1'. Rewrite it to the first available isolated tenant.
        if "/1/" in path or path.endswith("/1"):
            all_tnts = C.tenants.list_tenants()
            if all_tnts:
                tnt = all_tnts[0].id
                # Only rewrite specific base paths to avoid accidentally replacing numbers elsewhere
                bases = ["tenants", "junior", "senior", "triage", "stakeholder", "research", "billing"]
                for b in bases:
                    if path.startswith(f"/{b}/1/") or path == f"/{b}/1":
                        request.scope["path"] = path.replace(f"/{b}/1", f"/{b}/{tnt}", 1)
                        break
        return await call_next(request)

    async def _access_log_middleware(request: Request, call_next):"""

new_content = content.replace("    async def _access_log_middleware(request: Request, call_next):", middleware_code)

with open("analytics_platform/api.py", "w") as f:
    f.write(new_content)
print("api.py updated")
