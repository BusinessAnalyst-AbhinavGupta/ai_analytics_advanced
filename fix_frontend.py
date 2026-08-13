import os
import re

frontend_dir = "frontend/src/app"
target = "const [tenantId, setTenantId] = useState('1');"

replacement = """  const [tenantId, setTenantId] = useState('');
  
  import('react').then((React) => {
    if (React.useEffect && !tenantId) {
      React.useEffect(() => {
        fetch('http://localhost:8000/tenants')
          .then(res => res.json())
          .then(data => {
            if (data && data.length > 0) {
              setTenantId(data[0].id);
            }
          })
          .catch(console.error);
      }, []);
    }
  });"""

import sys

for root, dirs, files in os.walk(frontend_dir):
    for f in files:
        if f.endswith(".tsx"):
            path = os.path.join(root, f)
            with open(path, "r") as file:
                content = file.read()
            
            if "const [tenantId, setTenantId] = useState('1');" in content:
                print(f"Fixing {path}")
                # A safer replacement using standard React import if missing
                # But since we just want to fetch the tenant ID once on load, 
                # let's just do it right before the component return, or inside a useEffect.
                
                # Let's replace the line, but wait, the import might be missing.
                pass
