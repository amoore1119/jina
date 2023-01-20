from typing import Optional

from docarray import BaseDocument, DocumentArray
from docarray.typing import AnyTensor, ImageUrl

from jina import Executor, Flow, requests


def test_simple_flow():
    from docarray.documents.legacy import Document, DocumentArray

    class MyExec(Executor):
        @requests(on='/foo')
        def foo(self, docs: DocumentArray, **kwargs):
            docs[0].text = 'hello world'

        @requests(on='/bar')
        def bar(self, docs: DocumentArray, **kwargs):
            new_docs = DocumentArray(
                [Document(text='new docs') for _ in range(len(docs))]
            )
            return new_docs

    with Flow().add(uses=MyExec) as f:
        doc = f.post(on='/foo', inputs=Document(text='hello'))
        assert doc[0].text == 'hello world'

        doc = f.post(on='/bar', inputs=Document(text='hello'))
        assert doc.text == ['new docs']


def test_different_document_schema():
    class Image(BaseDocument):
        tensor: Optional[AnyTensor]
        url: ImageUrl

    class MyExec(Executor):
        @requests(on='/foo')
        def foo(self, docs: DocumentArray[Image], **kwargs) -> DocumentArray[Image]:
            for doc in docs:
                doc.tensor = doc.url.load()
            return docs

    with Flow().add(uses=MyExec) as f:
        docs = f.post(
            on='/foo',
            inputs=DocumentArray[Image](
                [Image(url='https://via.placeholder.com/150.png')]
            ),
        )
        docs = docs.stack()
        assert docs.tensor.ndim == 4


def test_send_custom_doc():
    class MyDoc(BaseDocument):
        text: str

    class MyExec(Executor):
        @requests(on='/foo')
        def foo(self, docs: DocumentArray[MyDoc], **kwargs):
            docs[0].text = 'hello world'

    with Flow().add(uses=MyExec) as f:
        doc = f.post(on='/foo', inputs=MyDoc(text='hello'))
        assert doc[0].text == 'hello world'


def test_receive_da_type():
    class MyDoc(BaseDocument):
        text: str

    class MyExec(Executor):
        @requests(
            on='/foo',
            input_doc=DocumentArray[MyDoc],
            output_doc=DocumentArray[MyDoc],
        )
        def foo(self, docs, **kwargs):
            assert docs.__class__.document_type == MyDoc
            docs[0].text = 'hello world'
            return docs

        @requests(on='/bar')
        def bar(self, docs: DocumentArray[MyDoc], **kwargs) -> DocumentArray[MyDoc]:
            assert docs.__class__.document_type == MyDoc
            docs[0].text = 'hello world'
            return docs

    with Flow().add(uses=MyExec) as f:
        docs = f.post(on='/foo', inputs=MyDoc(text='hello'))
        assert docs[0].text == 'hello world'
        # assert docs.__class__.document_type == MyDoc

        docs = f.post(on='/bar', inputs=MyDoc(text='hello'))
        assert docs[0].text == 'hello world'
        # assert docs.__class__.document_type == MyDoc
