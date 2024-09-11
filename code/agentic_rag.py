import os
from typing import List
from typing_extensions import TypedDict
from langchain.schema import Document
from langchain.prompts import PromptTemplate
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_core.output_parsers import StrOutputParser, JsonOutputParser
from langchain_community.document_loaders import WebBaseLoader
from langchain_community.vectorstores import Chroma
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_nvidia_ai_endpoints import NVIDIAEmbeddings, ChatNVIDIA
from langgraph.graph import END, StateGraph


BASE_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
MAIN_MODEL_ID = "meta/llama-3.1-70b-instruct"
MODEL_ID = "meta/llama-3.1-70b-instruct"


def build_rag_pipeline():
    docs = []
    folder_path = os.path.join(BASE_PATH, 'data/docs')
    for filename in os.listdir(folder_path):
        if filename.endswith('.txt'):
            file_path = os.path.join(folder_path, filename)
            with open(file_path, 'r') as f:
                text = f.read()
                docs.append(Document(page_content=text, metadata={'source': filename}))

    text_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=250,
        chunk_overlap=0,
    )
    doc_splits = text_splitter.split_documents(docs)
    vectorstore = Chroma.from_documents(
        documents=doc_splits,
        collection_name="rag-chroma",
        embedding=NVIDIAEmbeddings(model='NV-Embed-QA'),
    )
    retriever = vectorstore.as_retriever()


    # ASSISTANT MODEL
    prompt = PromptTemplate(
        template="""<|begin_of_text|><|start_header_id|>system<|end_header_id|> You are a personalized assistant for 
        financial advicing and investment management question-answering tasks. Use the following context from 
        retrieved documents to formulate a detailed and accurate answer without preluding that you used the provided 
        context. Also use the following statements from the user context to develop a deep understanding of the my 
        mindset for tailoring relevant financial plans that matches the my investor personality. Be helpful and 
        confident and clearly state your answer. Refrain from saying anything along the lines of "Please note that 
        this is a general recommendation and not a personalized investment advice. It's essential to consult with a 
        financial advisor or conduct your own research before making any investment decisions." If the question asks 
        about predicting a specific stock, at the end of your response, return a JSON with three keys. The first key 
        is `symbol` which is the trading symbol of the stock. The second key is `action` which is your choice of `buy`, 
        `hold`, or `sell`. The last key is 'days' which is an integer value representing the number of days to predict 
        this stock for. If the question did not ask about predicting a specific stock, return this JSON with "None" for 
        all values. Do not include any preamble or headline before or explanation after you return this JSON.
        <|eot_id|><|start_header_id|>user<|end_header_id|>
        Question: {question} 
        User Context: The following statements aim to provide an understanding of the user's investor personality (do not 
        take it literally, these are hypothetical statements to assess personality). Building wealth at the expense of my 
        current lifestyle best reflects my wealth goals. I would prefer to maintain control over my own investments over 
        delegating that responsibility to somebody else. My desire to preserve wealth is stronger than my tolerance for 
        risk to build wealth. I would take a 50/50 chance of either doubling my income or halfing my income. In my work 
        and personal life when something needs to be done, I generally prefer taking initiative rather than taking 
        directions. I believe in the idea of borrowing money to make money.
        Retrieved Documents: {context} 
        Answer: <|eot_id|><|start_header_id|>assistant<|end_header_id|>""",
        input_variables=["question", "document"],
    )
    llm = ChatNVIDIA(model=MAIN_MODEL_ID, temperature=0.5)
    rag_chain = prompt | llm | StrOutputParser()


    # QUESTION ROUTER
    prompt = PromptTemplate(
        template="""<|begin_of_text|><|start_header_id|>system<|end_header_id|> You are an expert at routing a 
        user question to a vectorstore or web search. Use the vectorstore for questions on LLM  agents, 
        prompt engineering, and adversarial attacks. You do not need to be stringent with the keywords 
        in the question related to these topics. Otherwise, use web-search. Give a binary choice 'web_search' 
        or 'vectorstore' based on the question. Return the JSON with a single key 'datasource' and no preamble
        or explanation. Question to route: {question} <|eot_id|><|start_header_id|>assistant<|end_header_id|>""",
        input_variables=["question"],
    )
    llm = ChatNVIDIA(model=MODEL_ID, temperature=0)
    question_router = prompt | llm | JsonOutputParser()


    # RETRIEVAL GRADER
    prompt = PromptTemplate(
        template="""<|begin_of_text|><|start_header_id|>system<|end_header_id|> You are a grader assessing relevance 
        of a retrieved document to a user question. If the document contains keywords related to the user question, 
        grade it as relevant. It does not need to be a stringent test. The goal is to filter out erroneous retrievals. \n
        Give a binary score 'yes' or 'no' score to indicate whether the document is relevant to the question. \n
        Provide the binary score as a JSON with a single key 'score' and no premable or explanation.
        <|eot_id|><|start_header_id|>user<|end_header_id|>
        Here is the retrieved document: \n\n {document} \n\n
        Here is the user question: {question} \n <|eot_id|><|start_header_id|>assistant<|end_header_id|>
        """,
        input_variables=["question", "document"],
    )
    llm = ChatNVIDIA(model=MODEL_ID, temperature=0)
    retrieval_grader = prompt | llm | JsonOutputParser()


    # HALLUCINATION GRADER
    prompt = PromptTemplate(
        template=""" <|begin_of_text|><|start_header_id|>system<|end_header_id|> You are a grader assessing whether 
        an answer is grounded in / supported by a set of facts. Give a binary 'yes' or 'no' score to indicate 
        whether the answer is grounded in / supported by a set of facts. Provide the binary score as a JSON with a 
        single key 'score' and no preamble or explanation. <|eot_id|><|start_header_id|>user<|end_header_id|>
        Here are the facts:
        \n ------- \n
        {documents} 
        \n ------- \n
        Here is the answer: {generation}  <|eot_id|><|start_header_id|>assistant<|end_header_id|>""",
        input_variables=["generation", "documents"],
    )
    llm = ChatNVIDIA(model=MODEL_ID, temperature=0)
    hallucination_grader = prompt | llm | JsonOutputParser()


    # ANSWER GRADER
    prompt = PromptTemplate(
        template="""<|begin_of_text|><|start_header_id|>system<|end_header_id|> You are a grader assessing whether an 
        answer is useful to resolve a question. Give a binary score 'yes' or 'no' to indicate whether the answer is 
        useful to resolve a question. Provide the binary score as a JSON with a single key 'score' and no preamble or explanation.
        <|eot_id|><|start_header_id|>user<|end_header_id|> Here is the answer:
        \n ------- \n
        {generation} 
        \n ------- \n
        Here is the question: {question} <|eot_id|><|start_header_id|>assistant<|end_header_id|>""",
        input_variables=["generation", "question"],
    )
    llm = ChatNVIDIA(model=MODEL_ID, temperature=0)
    answer_grader = prompt | llm | JsonOutputParser()


    # STATE
    class GraphState(TypedDict):
        """
        Represents the state of our graph.

        Attributes:
            question: question
            generation: LLM generation
            web_search: whether to add search
            documents: list of documents
        """
        question: str
        generation: str
        web_search: str
        documents: List[str]

    # NODES
    def web_search(state):
        """
        Web search based based on the question

        Args:
            state (dict): The current graph state

        Returns:
            state (dict): Appended web results to documents
        """

        print("---WEB SEARCH---")
        question = state["question"]
        documents = state["documents"] if "documents" in state.keys() else []

        # Web search
        docs = TavilySearchResults(k=3).invoke({"query": question})
        web_results = "\n".join([d["content"] for d in docs])
        web_results = Document(page_content=web_results)
        if documents is not None:
            documents.append(web_results)
        else:
            documents = [web_results]
        return {"documents": documents, "question": question}


    def retrieve(state):
        """
        Retrieve documents from vectorstore

        Args:
            state (dict): The current graph state

        Returns:
            state (dict): New key added to state, documents, that contains retrieved documents
        """
        print("---RETRIEVE---")
        question = state["question"]

        # Retrieval
        documents = retriever.invoke(question)
        return {"documents": documents, "question": question}


    def grade_documents(state):
        """
        Determines whether the retrieved documents are relevant to the question
        If any document is not relevant, we will set a flag to run web search

        Args:
            state (dict): The current graph state

        Returns:
            state (dict): Filtered out irrelevant documents and updated web_search state
        """

        print("---CHECK DOCUMENT RELEVANCE TO QUESTION---")
        question = state["question"]
        documents = state["documents"]

        # Score each doc
        filtered_docs = []
        web_search = "No"
        for d in documents:
            score = retrieval_grader.invoke(
                {"question": question, "document": d.page_content}
            )
            grade = score["score"]
            # Document relevant
            if grade.lower() == "yes":
                print("---GRADE: DOCUMENT RELEVANT---")
                filtered_docs.append(d)
            # Document not relevant
            else:
                print("---GRADE: DOCUMENT NOT RELEVANT---")
                # We do not include the document in filtered_docs
                # We set a flag to indicate that we want to run web search
                web_search = "Yes"
                continue
        return {"documents": filtered_docs, "question": question, "web_search": web_search}


    def generate(state):
        """
        Generate answer using RAG on retrieved documents

        Args:
            state (dict): The current graph state

        Returns:
            state (dict): New key added to state, generation, that contains LLM generation
        """
        print("---GENERATE---")
        question = state["question"]
        documents = state["documents"]

        # RAG generation
        generation = rag_chain.invoke({"context": documents, "question": question})
        return {"documents": documents, "question": question, "generation": generation}


    # CONDITIONAL EDGE
    def route_question(state):
        """
        Route question to web search or RAG.

        Args:
            state (dict): The current graph state

        Returns:
            str: Next node to call
        """

        print("---ROUTE QUESTION---")
        question = state["question"]
        print(question)
        source = question_router.invoke({"question": question})
        print(source)
        print(source["datasource"])
        if source["datasource"] == "web_search":
            print("---ROUTE QUESTION TO WEB SEARCH---")
            return "websearch"
        elif source["datasource"] == "vectorstore":
            print("---ROUTE QUESTION TO RAG---")
            return "vectorstore"


    def decide_to_generate(state):
        """
        Determines whether to generate an answer, or add web search

        Args:
            state (dict): The current graph state

        Returns:
            str: Binary decision for next node to call
        """

        print("---ASSESS GRADED DOCUMENTS---")
        question = state["question"]
        web_search = state["web_search"]
        filtered_documents = state["documents"]

        if web_search == "Yes":
            # All documents have been filtered check_relevance
            # We will re-generate a new query
            print(
                "---DECISION: ALL DOCUMENTS ARE NOT RELEVANT TO QUESTION, INCLUDE WEB SEARCH---"
            )
            return "websearch"
        else:
            # We have relevant documents, so generate answer
            print("---DECISION: GENERATE---")
            return "generate"


    # CONDITIONAL EDGE
    def grade_generation_v_documents_and_question(state):
        """
        Determines whether the generation is grounded in the document and answers question.

        Args:
            state (dict): The current graph state

        Returns:
            str: Decision for next node to call
        """

        print("---CHECK HALLUCINATIONS---")
        question = state["question"]
        documents = state["documents"]
        generation = state["generation"]

        score = hallucination_grader.invoke(
            {"documents": documents, "generation": generation}
        )
        grade = score["score"]

        # Check hallucination
        if grade == "yes":
            print("---DECISION: GENERATION IS GROUNDED IN DOCUMENTS---")
            # Check question-answering
            print("---GRADE GENERATION vs QUESTION---")
            score = answer_grader.invoke({"question": question, "generation": generation})
            grade = score["score"]
            if grade == "yes":
                print("---DECISION: GENERATION ADDRESSES QUESTION---")
                return "useful"
            else:
                print("---DECISION: GENERATION DOES NOT ADDRESS QUESTION---")
                return "not useful"
        else:
            print("---DECISION: GENERATION IS NOT GROUNDED IN DOCUMENTS, RE-TRY---")
            return "not supported"
    

    workflow = StateGraph(GraphState)

    # Define the nodes
    workflow.add_node("websearch", web_search)  # web search
    workflow.add_node("retrieve", retrieve)  # retrieve
    workflow.add_node("grade_documents", grade_documents)  # grade documents
    workflow.add_node("generate", generate)  # generate

    # Build graph
    workflow.set_conditional_entry_point(
        route_question,
        {
            "websearch": "websearch",
            "vectorstore": "retrieve",
        },
    )

    workflow.add_edge("retrieve", "grade_documents")
    workflow.add_conditional_edges(
        "grade_documents",
        decide_to_generate,
        {
            "websearch": "websearch",
            "generate": "generate",
        },
    )
    workflow.add_edge("websearch", "generate")
    workflow.add_conditional_edges(
        "generate",
        grade_generation_v_documents_and_question,
        {
            "not supported": "generate",
            "useful": END,
            "not useful": "websearch",
        },
    )

    return workflow


def ask(rag_agents, question):
    inputs = {"question": question}
    failure_count = 0
    for out in rag_agents.stream(inputs):
        for key, value in out.items():
            if key == 'generate':
                failure_count += 1
            if failure_count > 2:
                return "I am sorry. I am having trouble with that. Please try again.", 1
    return value["generation"], 0


import argparse
from pprint import pprint


if __name__ == "__main__":
    workflow = build_rag_pipeline()
    rag_agents = workflow.compile()

    parser = argparse.ArgumentParser(description="Ask a question.")
    parser.add_argument(
        "-q",
        "--question",
        required=True,
        type=str,
        help="The question to be answered"
    )
    args = parser.parse_args()

    inputs = {"question": args.question}
    for out in rag_agents.stream(inputs):
        for key, value in out.items():
            pprint(f"Finished running: {key}:")
    pprint(value["generation"])
