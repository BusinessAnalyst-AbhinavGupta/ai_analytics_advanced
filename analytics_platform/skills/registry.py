import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

@dataclass
class SkillMetaData:
    name: str
    description: str
    skill_dir: str

@dataclass
class SkillBundle:
    meta: SkillMetaData
    instructions: str
    sql_templates: Dict[str, str] = field(default_factory=dict)

class SkillRegistry:
    def __init__(self, base_path: str = ".agents/skills"):
        self.base_path = Path(base_path)
        self.skills: Dict[str, SkillBundle] = {}
        self.meta: List[SkillMetaData] = []
        
    def load_skills(self) -> List[SkillMetaData]:
        if not self.base_path.exists() or not self.base_path.is_dir():
            return []
            
        self.skills.clear()
        self.meta.clear()
        
        for skill_dir in self.base_path.iterdir():
            if not skill_dir.is_dir():
                continue
                
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
                
            try:
                content = skill_md.read_text(encoding="utf-8")
                name, description = self._parse_frontmatter(content)
                if not name:
                    continue
                    
                meta = SkillMetaData(name=name, description=description, skill_dir=str(skill_dir))
                
                # Load SQL templates
                sql_templates = {}
                ref_dir = skill_dir / "references"
                if ref_dir.exists() and ref_dir.is_dir():
                    for sql_file in ref_dir.glob("*.sql"):
                        sql_templates[sql_file.name] = sql_file.read_text(encoding="utf-8")
                        
                bundle = SkillBundle(meta=meta, instructions=content, sql_templates=sql_templates)
                self.skills[name] = bundle
                self.meta.append(meta)
                
            except Exception as e:
                # Log error or continue
                pass
                
        return self.meta

    def get_skill(self, name: str) -> Optional[SkillBundle]:
        return self.skills.get(name)
        
    def _parse_frontmatter(self, content: str) -> tuple[str, str]:
        name = ""
        description = ""
        
        # Match frontmatter between --- and ---
        match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
        if match:
            frontmatter = match.group(1)
            # Extract name
            name_match = re.search(r'^name:\s*(.+)$', frontmatter, re.MULTILINE)
            if name_match:
                name = name_match.group(1).strip()
            
            # Extract description
            desc_match = re.search(r'^description:\s*(.+)$', frontmatter, re.MULTILINE)
            if desc_match:
                description = desc_match.group(1).strip()
                
        return name, description
