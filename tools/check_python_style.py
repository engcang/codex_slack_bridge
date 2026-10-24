"""Check the project's Python naming and visual argument alignment."""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

OVERRIDES = {"do_POST", "get_request", "log_message", "redirect_request"}


def checkFile(_path: Path) -> list[str]:
    source = _path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_path))
    errors = []
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))

    def report(_line: int,
               _message: str):
        errors.append(f"{_path}:{_line}: {_message}")

    for token in tokens:
        if token.type == tokenize.INDENT and ("\t" in token.string or len(token.string) % 4):
            report(token.start[0], "Use four-space block indentation")

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fixture = any(isinstance(item, ast.Attribute) and item.attr == "fixture"
                          or isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
                          and item.func.attr == "fixture" for item in node.decorator_list)
            test = _path.parts[0] == "tests" and node.name.startswith("test_")
            if not (test or node.name in OVERRIDES or node.name.startswith("__")):
                if not re.fullmatch(r"[a-z][a-zA-Z0-9]*", node.name):
                    report(node.lineno, "Use camelCase function names")
            parameters = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            parameters.extend(item for item in (node.args.vararg, node.args.kwarg) if item)
            parameters.sort(key=lambda _item: (_item.lineno, _item.col_offset))
            if not (fixture or test):
                for parameter in parameters:
                    if not re.fullmatch(r"_[a-z][a-z0-9_]*", parameter.arg):
                        report(parameter.lineno, "Prefix function parameters with an underscore")
            for first, following in zip(parameters, parameters[1:]):
                if first.lineno == following.lineno:
                    report(following.lineno, "Put declaration parameters on separate aligned lines")
            if parameters:
                first_stars = 2 if parameters[0] is node.args.kwarg else 1 if parameters[0] is node.args.vararg else 0
                initial_column = parameters[0].col_offset - first_stars
                for parameter in parameters[1:]:
                    stars = 2 if parameter is node.args.kwarg else 1 if parameter is node.args.vararg else 0
                    if parameter.col_offset - stars != initial_column:
                        report(parameter.lineno, "Align declaration parameters with the first parameter")
        elif isinstance(node, ast.ClassDef):
            if not re.fullmatch(r"[A-Z][a-zA-Z0-9]*", node.name):
                report(node.lineno, "Use PascalCase class names")
            for statement in node.body:
                if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                    if not re.fullmatch(r"[a-z][a-z0-9_]*_", statement.target.id):
                        report(statement.lineno, "Suffix first-party data members with an underscore")
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            if isinstance(node.value, ast.Name) and node.value.id == "_self":
                if not re.fullmatch(r"[a-z][a-z0-9_]*_", node.attr):
                    report(node.lineno, "Suffix first-party data members with an underscore")
        elif isinstance(node, ast.Call) and (node.args or node.keywords):
            after_function = (node.func.end_lineno, node.func.end_col_offset)
            opening_index = next(index for index, token in enumerate(tokens)
                                 if token.start >= after_function and token.string == "(")
            opening = tokens[opening_index]
            content = next(token for token in tokens[opening_index + 1:]
                           if token.type not in (tokenize.NL, tokenize.COMMENT))
            if content.start != opening.end:
                report(opening.start[0], "Keep the first call argument immediately after '('")
            arguments = sorted([*node.args, *node.keywords], key=lambda _item: (_item.lineno, _item.col_offset))
            for previous, argument in zip(arguments, arguments[1:]):
                if argument.lineno > previous.lineno and argument.col_offset != content.start[1]:
                    report(argument.lineno, "Align continuation arguments with the first argument")
                elif node.end_lineno > opening.start[0] and argument.lineno == previous.lineno:
                    report(argument.lineno, "Put wrapped call arguments on separate lines")
            if node.args and isinstance(node.args[0], (ast.List, ast.Tuple, ast.Set, ast.Dict)):
                collection = node.args[0]
                elements = collection.elts if hasattr(collection, "elts") else [key for key in collection.keys if key]
                if elements and elements[0].lineno != collection.lineno:
                    report(collection.lineno, "Keep the first collection element after its delimiter")
    return errors


def main(_arguments: list[str]) -> int:
    errors = []
    for filename in _arguments:
        errors.extend(checkFile(Path(filename)))
    for error in errors:
        print(error)
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
