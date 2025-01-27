# © 2024 Thoughtworks, Inc. | Licensed under the Apache License, Version 2.0  | See LICENSE.md file for permissions.
import json
import time
import uuid
import os
import pandas as pd

from pydantic import BaseModel
from config_service import ConfigService
from knowledge_manager import KnowledgeManager
from embeddings.documents import DocumentsUtils
from llms.clients import (
    ChatClient,
    ChatClientFactory,
    HaivenAIMessage,
    HaivenHumanMessage,
    HaivenSystemMessage,
    ModelConfig,
)
from logger import HaivenLogger


class HaivenBaseChat:
    def __init__(
        self,
        chat_client: ChatClient,
        knowledge_manager: KnowledgeManager,
        system_message: str,
    ):
        self.system = system_message
        self.memory = [HaivenSystemMessage(content=system_message)]
        self.chat_client = chat_client
        self.knowledge_manager = knowledge_manager

    def log_run(self, extra={}):
        class_name = self.__class__.__name__
        extra_info = {
            "chat_type": class_name,
            "numberOfMessages": len(self.memory),
        }
        extra_info.update(extra)

        HaivenLogger.get().analytics("Sending message", extra_info)

    def memory_as_text(self):
        return "\n".join([str(message) for message in self.memory])

    def _similarity_query(self, message):
        if len(self.memory) == 1:
            return message

        if len(self.memory) > 5:
            conversation = "\n".join(
                [message.content for message in (self.memory[:2] + self.memory[-4:])]
            )
        else:
            conversation = "\n".join([message.content for message in self.memory])

        system_message = f"""You are a helpful assistant.
        Your task is create a single search query to find relevant information, based on the conversation and the current user message.
        Rules: 
        - Search query should find relevant information for the current user message only.
        - Include all important key words and phrases in query that would help to search for relevant information.
        - If the current user message does not need to search for additional information, return NONE.
        - Only return the single standalone search query or NONE. No explanations needed.
        
        Conversation:
        {conversation}
        """
        prompt = [HaivenSystemMessage(content=system_message)]
        prompt.append(
            HaivenHumanMessage(content=f"Current user message: {message} \n Query:")
        )

        stream = self.chat_client.stream(prompt)
        query = ""
        for chunk in stream:
            query += chunk["content"]

        if "none" in query.lower():
            return None
        elif "query:" in query.lower():
            return query.split("query:")[1].strip()
        else:
            return query

    def _similarity_search_based_on_history(self, message, knowledge_document_key):
        similarity_query = self._similarity_query(message)
        print("Similarity Query:", similarity_query)
        if similarity_query is None:
            return None, None

        if knowledge_document_key:
            knowledge_document = (
                self.knowledge_manager.knowledge_base_documents.get_document(
                    knowledge_document_key
                )
            )
            context_documents = self.knowledge_manager.knowledge_base_documents.similarity_search_on_single_document(
                query=similarity_query,
                document_key=knowledge_document.key,
                context=knowledge_document.context,
            )
        else:
            return None, None

        if os.getenv("IS_EVALUATION", "false").lower() == "true":
            try:
                scores = [doc.metadata.get("score", 0) for doc in context_documents]
                page_contents = [doc.page_content for doc in context_documents]
                csv_path = os.getenv("EVALS_DATA_FILE_PATH")
                df = pd.read_csv(csv_path)

                if "similarity_query" not in df.columns:
                    df.insert(
                        loc=len(df.columns),
                        column="similarity_query",
                        value=[None] * len(df),
                    )
                if "retrieved_contexts" not in df.columns:
                    df.insert(
                        loc=len(df.columns),
                        column="retrieved_contexts",
                        value=[None] * len(df),
                    )
                if "scores" not in df.columns:
                    df.insert(
                        loc=len(df.columns), column="scores", value=[None] * len(df)
                    )

                for idx in range(len(df)):
                    if pd.isna(df.at[idx, "similarity_query"]):
                        df.at[idx, "similarity_query"] = similarity_query
                        df.at[idx, "retrieved_contexts"] = str(page_contents)
                        df.at[idx, "scores"] = str(scores)
                        break

                df.to_csv(csv_path, index=False)

            except Exception as e:
                print(f"Error writing retrieval evaluation data: {str(e)}")

        context_for_prompt = "\n---".join(
            [f"{document.page_content}" for document in context_documents]
        )
        de_duplicated_sources = DocumentsUtils.get_unique_sources(context_documents)
        sources_markdown = (
            "**These articles might be relevant:**\n"
            + "\n".join(
                [
                    f"- {DocumentsUtils.get_search_result_item(source.metadata)}"
                    for source in de_duplicated_sources
                ]
            )
            + "\n\n"
        )

        return context_for_prompt, sources_markdown


class StreamingChat(HaivenBaseChat):
    def __init__(
        self,
        chat_client: ChatClient,
        knowledge_manager: KnowledgeManager,
        system_message: str = "You are a helpful assistant",
        stream_in_chunks: bool = False,
    ):
        super().__init__(chat_client, knowledge_manager, system_message)
        self.stream_in_chunks = stream_in_chunks

    def run(self, message: str, user_query: str = None):
        self.memory.append(HaivenHumanMessage(content=message))
        try:
            for i, chunk in enumerate(self.chat_client.stream(self.memory)):
                if i == 0:
                    if user_query:
                        self.memory[-1].content = user_query
                    self.memory.append(HaivenAIMessage(content=""))
                self.memory[-1].content += chunk["content"]
                yield chunk["content"]

        except Exception as error:
            if not str(error).strip():
                error = "Error while the model was processing the input"
            print(f"[ERROR]: {str(error)}")
            yield f"[ERROR]: {str(error)}"

    def run_with_document(
        self,
        knowledge_document_key: str,
        message: str = None,
    ):
        try:
            context_for_prompt, sources_markdown = (
                self._similarity_search_based_on_history(
                    message, knowledge_document_key
                )
            )

            user_request = (
                message
                or "Based on our conversation so far, what do you think is relevant to me with the CONTEXT information I gathered?"
            )

            if context_for_prompt:
                prompt = f"""
                {user_request}
                ---- Here is some additional CONTEXT that might be relevant to this:
                {context_for_prompt} 
                -------
                Do not provide any advice that is outside of the CONTEXT I provided.
                """
            else:
                prompt = user_request

            for chunk in self.run(prompt, user_request):
                yield chunk, sources_markdown

        except Exception as error:
            if not str(error).strip():
                error = "Error while the model was processing the input"
            print(f"[ERROR]: {str(error)}")
            yield f"[ERROR]: {str(error)}", ""


class JSONChat(HaivenBaseChat):
    def __init__(
        self,
        chat_client: ChatClient,
        system_message: str = "You are a helpful assistant",
        event_stream_standard=True,
    ):
        super().__init__(chat_client, None, system_message)
        # Transition to new frontend SSE implementation: Add "data: " and "[DONE]" vs not doing that
        self.event_stream_standard = event_stream_standard

    def stream_from_model(self, new_message):
        try:
            self.memory.append(HaivenHumanMessage(content=new_message))
            stream = self.chat_client.stream(self.memory)
            for chunk in stream:
                yield chunk["content"]

            if self.event_stream_standard:
                yield "[DONE]"

        except Exception as error:
            if not str(error).strip():
                error = "Error while the model was processing the input"
            print(f"[ERROR]: {str(error)}")
            yield f"[ERROR]: {str(error)}"

    def run(self, message: str):
        try:
            data = enumerate(self.stream_from_model(message))
            for i, chunk in data:
                if i == 0:
                    self.memory.append(HaivenAIMessage(content=""))

                if chunk == "[DONE]":
                    yield f"data: {chunk}\n\n"
                else:
                    self.memory[-1].content += chunk
                    if self.event_stream_standard:
                        message = '{ "data": ' + json.dumps(chunk) + " }"
                        yield f"data: {message}\n\n"
                    else:
                        message = json.dumps({"data": chunk})
                        yield f"{message}\n\n"

        except Exception as error:
            if not str(error).strip():
                error = "Error while the model was processing the input"
            print(f"[ERROR]: {str(error)}")
            yield f"[ERROR]: {str(error)}"


class ServerChatSessionMemory:
    def __init__(self):
        self.USER_CHATS = {}

    def clear_old_entries(self):
        allowed_age_in_minutes = 30
        allowed_age_in_seconds = 60 * allowed_age_in_minutes
        print(
            f"CLEANUP: Removing chat sessions with age > {allowed_age_in_minutes} mins from memory. Currently {len(self.USER_CHATS)} entries in memory"
        )

        entries_to_remove = list(
            filter(
                lambda key: self.USER_CHATS[key]["last_access"]
                < time.time() - allowed_age_in_seconds,
                self.USER_CHATS,
            )
        )

        for key in entries_to_remove:
            print("CLEANUP: Removing entry", key)
            del self.USER_CHATS[key]

    def add_new_entry(self, category: str, user_identifier: str):
        # Currently the crude rhythm of checking for old entries: whenever a new one gets created
        self.clear_old_entries()

        session_key = category + "-" + str(uuid.uuid4())

        HaivenLogger.get().analytics(
            f"Creating a new chat session for category {category} with key {session_key} for user {user_identifier}"
        )
        self.USER_CHATS[session_key] = {
            "created_at": time.time(),
            "last_access": time.time(),
            "user": user_identifier,
            "chat": None,
        }
        return session_key

    def store_chat(self, session_key: str, chat_session: HaivenBaseChat):
        self.USER_CHATS[session_key]["chat"] = chat_session

    def get_chat(self, session_key: str):
        self.clear_old_entries()
        if session_key not in self.USER_CHATS:
            raise ValueError(
                f"Invalid identifier {session_key}, your chat session might have expired"
            )
        self.USER_CHATS[session_key]["last_access"] = time.time()
        return self.USER_CHATS[session_key]["chat"]

    def delete_entry(self, session_key):
        if session_key in self.USER_CHATS:
            print("Discarding a chat session from memory", session_key)
            del self.USER_CHATS[session_key]

    def get_or_create_chat(
        self,
        fn_create_chat,
        chat_session_key_value: str = None,
        chat_category: str = "unknown",
        user_identifier: str = "unknown",
    ):
        if chat_session_key_value is None or chat_session_key_value == "":
            chat_session_key_value = self.add_new_entry(chat_category, user_identifier)
            chat_session = fn_create_chat()

            self.store_chat(chat_session_key_value, chat_session)
        else:
            chat_session = self.get_chat(chat_session_key_value)

        return chat_session_key_value, chat_session

    def dump_as_text(self, session_key: str, user_owner: str):
        chat_session_data = self.USER_CHATS.get(session_key, None)
        if chat_session_data is None:
            return f"Chat session with ID {session_key} not found"
        if chat_session_data["user"] != user_owner:
            return f"Chat session with ID {session_key} not found for this user"

        chat_session: HaivenBaseChat = chat_session_data["chat"]
        return chat_session.memory_as_text()


class ChatOptions(BaseModel):
    category: str = None
    in_chunks: bool = False
    user_identifier: str = None


class ChatManager:
    def __init__(
        self,
        config_service: ConfigService,
        chat_session_memory: ServerChatSessionMemory,
        llm_chat_factory: ChatClientFactory,
        knowledge_manager: KnowledgeManager,
    ):
        self.config_service = config_service
        self.chat_session_memory = chat_session_memory
        self.llm_chat_factory = llm_chat_factory
        self.knowledge_manager = knowledge_manager

    def clear_session(self, session_id: str):
        self.chat_session_memory.delete_entry(session_id)

    def get_session(self, chat_session_key_value):
        return self.chat_session_memory.get_chat(chat_session_key_value)

    def streaming_chat(
        self,
        model_config: ModelConfig,
        session_id: str = None,
        options: ChatOptions = None,
    ):
        chat_client = self.llm_chat_factory.new_chat_client(model_config)
        return self.chat_session_memory.get_or_create_chat(
            lambda: StreamingChat(
                chat_client,
                self.knowledge_manager,
                stream_in_chunks=options.in_chunks if options else None,
            ),
            chat_session_key_value=session_id,
            chat_category=options.category if options else None,
            user_identifier=options.user_identifier if options else None,
        )

    def json_chat(
        self,
        model_config: ModelConfig,
        session_id: str = None,
        options: ChatOptions = None,
    ):
        chat_client = self.llm_chat_factory.new_chat_client(model_config)
        return self.chat_session_memory.get_or_create_chat(
            lambda: JSONChat(
                chat_client,
                event_stream_standard=False,
            ),
            chat_session_key_value=session_id,
            chat_category=options.category if options else None,
            user_identifier=options.user_identifier if options else None,
        )
