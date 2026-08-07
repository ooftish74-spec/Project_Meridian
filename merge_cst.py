import libcst as cst
from libcst.metadata import PositionProvider

class DocstringExtractor(cst.CSTVisitor):
    def __init__(self):
        self.docstrings = {}

    def visit_ClassDef(self, node: cst.ClassDef):
        doc = node.get_docstring()
        if doc:
            self.docstrings[node.name.value] = doc

    def visit_FunctionDef(self, node: cst.FunctionDef):
        doc = node.get_docstring()
        if doc:
            self.docstrings[node.name.value] = doc

class DocstringInjector(cst.CSTTransformer):
    def __init__(self, docstrings):
        self.docstrings = docstrings

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef):
        doc = self.docstrings.get(original_node.name.value)
        if doc and not original_node.get_docstring():
            # Create docstring node
            doc_node = cst.SimpleStatementLine(body=[cst.Expr(value=cst.SimpleString(value=f'"""{doc}"""'))])
            new_body = [doc_node] + list(updated_node.body.body)
            return updated_node.with_changes(body=updated_node.body.with_changes(body=new_body))
        return updated_node

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef):
        doc = self.docstrings.get(original_node.name.value)
        if doc and not original_node.get_docstring():
            doc_node = cst.SimpleStatementLine(body=[cst.Expr(value=cst.SimpleString(value=f'"""{doc}"""'))])
            new_body = [doc_node] + list(updated_node.body.body)
            return updated_node.with_changes(body=updated_node.body.with_changes(body=new_body))
        return updated_node

def merge_docstrings(source_path, target_path):
    with open(source_path, 'r') as f:
        source_tree = cst.parse_module(f.read())
    
    extractor = DocstringExtractor()
    source_tree.visit(extractor)
    
    with open(target_path, 'r') as f:
        target_tree = cst.parse_module(f.read())
        
    injector = DocstringInjector(extractor.docstrings)
    modified_tree = target_tree.visit(injector)
    
    with open(target_path, 'w') as f:
        f.write(modified_tree.code)

merge_docstrings('shadow_manager_restored.py', 'src/portfolio/shadow_manager.py')
