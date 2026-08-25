from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

LANGUAGES = {
    ".py": "python",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".h": "cpp",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".md": "text",
    ".toml": "text",
    ".yaml": "text",
    ".yml": "text",
    ".json": "text",
}

SYMBOL_NODES = {
    "python": {
        "function_definition": "function",
        "class_definition": "class",
    },
    "java": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "method_declaration": "method",
        "constructor_declaration": "constructor",
    },
    "javascript": {
        "function_declaration": "function",
        "method_definition": "method",
        "class_declaration": "class",
        "generator_function_declaration": "function",
    },
    "typescript": {
        "function_declaration": "function",
        "method_definition": "method",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
    },
    "cpp": {
        "function_definition": "function",
        "class_specifier": "class",
        "struct_specifier": "struct",
        "namespace_definition": "namespace",
    },
}

IMPORT_PATTERN = re.compile(
    r"^\s*(?:from\s+[\w.]+\s+import|import\s+|#include\s*[<\"]|require\s*\(|"
    r"(?:export\s+)?import\s+)",
    re.MULTILINE,
)


@dataclass(slots=True)
class CodeChunk:
    id: str
    repository_id: str
    commit_sha: str
    file_path: str
    language: str
    symbol_name: str | None
    symbol_kind: str | None
    start_line: int
    end_line: int
    content: str
    content_sha256: str
    imports_json: str
    embedding_model: str
    embedding_version: str
    chunking_strategy: str
    vector_json: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class CodeChunker:
    def __init__(self, max_lines: int = 180, fallback_lines: int = 80, overlap: int = 10) -> None:
        self.max_lines = max_lines
        self.fallback_lines = fallback_lines
        self.overlap = overlap

    def language_for(self, path: Path) -> str:
        return LANGUAGES.get(path.suffix.lower(), "text")

    def chunk(
        self,
        repository_id: str,
        commit_sha: str,
        file_path: str,
        content: str,
        embedding_model: str,
        embedding_version: str,
    ) -> list[CodeChunk]:
        language = self.language_for(Path(file_path))
        if language in SYMBOL_NODES:
            try:
                chunks = self._tree_sitter_chunks(
                    repository_id,
                    commit_sha,
                    file_path,
                    content,
                    language,
                    embedding_model,
                    embedding_version,
                )
                if chunks:
                    return chunks
            except Exception:
                pass
        return self._fallback_chunks(
            repository_id,
            commit_sha,
            file_path,
            content,
            language,
            embedding_model,
            embedding_version,
        )

    def _tree_sitter_chunks(
        self,
        repository_id: str,
        commit_sha: str,
        file_path: str,
        content: str,
        language: str,
        embedding_model: str,
        embedding_version: str,
    ) -> list[CodeChunk]:
        source = content.encode("utf-8")
        tree = get_parser(language).parse(source)  # type: ignore[arg-type]
        if tree.root_node.has_error and not tree.root_node.named_children:
            return []
        candidates = []
        stack = [tree.root_node]
        symbol_types = SYMBOL_NODES[language]
        while stack:
            node = stack.pop()
            if node.type in symbol_types:
                name_node = node.child_by_field_name("name")
                if name_node is None and language == "cpp":
                    declarator = node.child_by_field_name("declarator")
                    name_node = self._find_identifier(declarator) if declarator else None
                name = (
                    source[name_node.start_byte : name_node.end_byte].decode("utf-8")
                    if name_node
                    else None
                )
                candidates.append(
                    (node.start_point[0] + 1, node.end_point[0] + 1, name, symbol_types[node.type])
                )
            stack.extend(reversed(node.named_children))
        candidates.sort(key=lambda item: (item[0], item[1]))
        chunks = []
        for start, end, name, kind in candidates:
            if end - start + 1 > self.max_lines:
                chunks.extend(
                    self._line_windows(
                        repository_id,
                        commit_sha,
                        file_path,
                        content,
                        language,
                        embedding_model,
                        embedding_version,
                        start,
                        end,
                        name,
                        kind,
                        "tree-sitter-split",
                    )
                )
            else:
                chunks.append(
                    self._make_chunk(
                        repository_id,
                        commit_sha,
                        file_path,
                        content,
                        language,
                        embedding_model,
                        embedding_version,
                        start,
                        end,
                        name,
                        kind,
                        "tree-sitter",
                    )
                )
        return chunks

    def _find_identifier(self, node: Node) -> Node | None:
        if getattr(node, "type", None) in {
            "identifier",
            "field_identifier",
            "qualified_identifier",
        }:
            return node
        for child in getattr(node, "named_children", []):
            found = self._find_identifier(child)
            if found:
                return found
        return None

    def _fallback_chunks(
        self,
        repository_id: str,
        commit_sha: str,
        file_path: str,
        content: str,
        language: str,
        embedding_model: str,
        embedding_version: str,
    ) -> list[CodeChunk]:
        line_count = max(1, len(content.splitlines()))
        return self._line_windows(
            repository_id,
            commit_sha,
            file_path,
            content,
            language,
            embedding_model,
            embedding_version,
            1,
            line_count,
            None,
            None,
            "line-fallback",
        )

    def _line_windows(
        self,
        repository_id: str,
        commit_sha: str,
        file_path: str,
        content: str,
        language: str,
        embedding_model: str,
        embedding_version: str,
        first: int,
        last: int,
        name: str | None,
        kind: str | None,
        strategy: str,
    ) -> list[CodeChunk]:
        chunks = []
        step = max(1, self.fallback_lines - self.overlap)
        start = first
        while start <= last:
            end = min(last, start + self.fallback_lines - 1)
            chunks.append(
                self._make_chunk(
                    repository_id,
                    commit_sha,
                    file_path,
                    content,
                    language,
                    embedding_model,
                    embedding_version,
                    start,
                    end,
                    name,
                    kind,
                    strategy,
                )
            )
            if end == last:
                break
            start += step
        return chunks

    def _make_chunk(
        self,
        repository_id: str,
        commit_sha: str,
        file_path: str,
        content: str,
        language: str,
        embedding_model: str,
        embedding_version: str,
        start: int,
        end: int,
        name: str | None,
        kind: str | None,
        strategy: str,
    ) -> CodeChunk:
        import json

        lines = content.splitlines(keepends=True)
        selected = "".join(lines[start - 1 : end]).rstrip()
        content_hash = hashlib.sha256(selected.encode()).hexdigest()
        identity = f"{repository_id}:{file_path}:{start}:{end}:{content_hash}"
        chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
        imports = [line.strip() for line in selected.splitlines() if IMPORT_PATTERN.match(line)][
            :50
        ]
        return CodeChunk(
            id=chunk_id,
            repository_id=repository_id,
            commit_sha=commit_sha,
            file_path=file_path.replace("\\", "/"),
            language=language,
            symbol_name=name,
            symbol_kind=kind,
            start_line=start,
            end_line=end,
            content=selected,
            content_sha256=content_hash,
            imports_json=json.dumps(imports),
            embedding_model=embedding_model,
            embedding_version=embedding_version,
            chunking_strategy=strategy,
        )
