import os
import time
import streamlit as st
import google.generativeai as genai
from dotenv import load_dotenv
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.document_loaders import UnstructuredURLLoader, SeleniumURLLoader
from langchain.vectorstores import Chroma
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.memory import ConversationBufferMemory
from datetime import datetime
import pandas as pd
import plotly.express as px
from bs4 import BeautifulSoup
import requests
import json
import uuid
import shutil
import atexit
import torch

# Load environment variables
load_dotenv()
GEMINI_API_KEY_new = os.getenv("GEMINI_API_KEY_new")

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY_new)
model = genai.GenerativeModel("gemini-1.5-pro-latest")

# App title and config
st.set_page_config(page_title="SaraBot AI: Advanced Search Tool", page_icon="🤖", layout="wide")
st.title("SaraBot AI: Advanced Search Tool 🤖")

# Initialize embeddings with device handling fix
try:
    embeddings = HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2",
        model_kwargs={'device': 'cpu'},  # Forced to CPU to avoid meta tensor issue
        encode_kwargs={'normalize_embeddings': True}
    )
except Exception as e:
    st.error(f"Failed to initialize embeddings: {str(e)}")
    embeddings = None

# Cleanup function for Windows file locking issues
def cleanup_chroma_db():
    if 'db_path' in globals() and os.path.exists(db_path):
        try:
            shutil.rmtree(db_path, ignore_errors=True)
        except:
            pass

atexit.register(cleanup_chroma_db)

# Initialize session state variables
if 'conversation_history' not in st.session_state:
    st.session_state.conversation_history = []
if 'processed_urls' not in st.session_state:
    st.session_state.processed_urls = []
if 'memory' not in st.session_state:
    st.session_state.memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)
if 'session_id' not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())[:8]

# Get database path based on current session
db_path = f"./chroma_db_{st.session_state.session_id}"

# Sidebar configuration
with st.sidebar:
    st.title("Configuration")
    
    # Session management
    with st.expander("🔧 Session Management", expanded=True):
        if st.button("New Session"):
            st.session_state.session_id = str(uuid.uuid4())[:8]
            st.session_state.processed_urls = []
            st.session_state.conversation_history = []
            st.session_state.memory.clear()
            st.rerun()
        
        st.info(f"Session ID: {st.session_state.session_id}")
    
    # URL input with expandable section
    with st.expander("📝 Article URLs", expanded=True):
        url_input_method = st.radio("URL Input Method", ["Manual Entry", "File Upload"])
        
        if url_input_method == "Manual Entry":
            urls = []
            for i in range(3):
                url = st.text_input(f"URL {i+1}", key=f"url_{i}")
                if url:
                    urls.append(url)
        else:
            uploaded_file = st.file_uploader("Upload text file with URLs or content", type=["txt"])
            urls = []
    
    # Advanced settings
    with st.expander("⚙️ Advanced Settings"):
        chunk_size = st.slider("Chunk Size", 500, 2000, 1000, step=100)
        chunk_overlap = st.slider("Chunk Overlap", 0, 500, 100, step=50)
        temperature = st.slider("Model Temperature", 0.0, 1.0, 0.7, step=0.1)
        max_results = st.slider("Max Results to Return", 1, 10, 3)
        use_selenium = st.checkbox("Use Selenium (for JavaScript-heavy sites)", False)
    
    process_url_clicked = st.button("Process URLs/Text", type="primary")
    st.markdown("---")
    st.markdown("### Conversation History")
    for i, chat in enumerate(st.session_state.conversation_history):
        st.caption(f"Q{i+1}: {chat['question'][:50]}...")

# Function to fetch article metadata
def get_article_metadata(url):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        title = soup.find('meta', property='og:title') or soup.find('meta', attrs={'name': 'title'}) or soup.title
        title = title['content'] if hasattr(title, 'has_attr') and title.has_attr('content') else str(title.string) if title else "Untitled Article"
        
        description = soup.find('meta', property='og:description') or soup.find('meta', attrs={'name': 'description'})
        description = description['content'] if description else "No description available"
        
        return {
            "title": title[:200] + "..." if len(title) > 200 else title,
            "description": description[:200] + "..." if len(description) > 200 else description,
            "url": url,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    except Exception as e:
        return {
            "title": "Untitled Article",
            "description": f"Error fetching metadata: {str(e)}",
            "url": url,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

# Function to call Gemini API with enhanced error handling
def generate_gemini_response(prompt, context=None):
    try:
        generation_config = {
            "temperature": temperature,
            "top_p": 0.95,
            "top_k": 40,
            "max_output_tokens": 2048,
        }
        
        if context:
            prompt = f"Context:\n{context}\n\nQuestion:\n{prompt}\n\nAnswer:"
        
        response = model.generate_content(
            prompt,
            generation_config=generation_config,
            stream=False
        )
        
        # Add to conversation history
        st.session_state.memory.save_context(
            {"input": prompt},
            {"output": response.text}
        )
        
        return response.text
    except Exception as e:
        return f"Error generating response: {str(e)}"

# Function to process URLs/text and create vector store
def process_urls(url_list, uploaded_file=None):
    if not any(url_list) and not uploaded_file:
        st.sidebar.warning("Please enter at least one valid URL or upload a text file.")
        return None
    
    with st.spinner("Processing content... This may take a few moments ⏳"):
        # Clear existing database if it exists
        if os.path.exists(db_path):
            try:
                shutil.rmtree(db_path, ignore_errors=True)
            except Exception as e:
                st.error(f"Error clearing old database: {str(e)}")
                return False
        
        # Reset processed URLs for this session
        st.session_state.processed_urls = []
        
        try:
            # Handle text file upload
            if uploaded_file:
                file_content = uploaded_file.read().decode("utf-8")
                metadata = {
                    "title": uploaded_file.name,
                    "description": "Uploaded text content",
                    "url": "local_file",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                st.session_state.processed_urls.append(metadata)
                
                # Split text
                text_splitter = RecursiveCharacterTextSplitter(
                    separators=['\n\n', '\n', '.', ','],
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap
                )
                docs = text_splitter.create_documents([file_content])
                
                # Create vector store
                vectorstore_chroma = Chroma.from_documents(
                    docs, 
                    embeddings, 
                    persist_directory=db_path
                )
                vectorstore_chroma.persist()
                
                st.success("Text content processed successfully! ✅")
                return True
            
            # Handle URLs
            elif url_list:
                valid_urls = []
                for url in url_list:
                    if url.startswith(('http://', 'https://')):
                        valid_urls.append(url)
                    else:
                        st.warning(f"Skipping invalid URL: {url}")
                
                if not valid_urls:
                    st.error("No valid URLs to process")
                    return False
                
                if use_selenium:
                    loader = SeleniumURLLoader(urls=valid_urls)
                else:
                    loader = UnstructuredURLLoader(urls=valid_urls)
                
                data = loader.load()
                
                # Store metadata for processed URLs
                for url in valid_urls:
                    metadata = get_article_metadata(url)
                    st.session_state.processed_urls.append(metadata)
                
                # Split documents
                text_splitter = RecursiveCharacterTextSplitter(
                    separators=['\n\n', '\n', '.', ','],
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap
                )
                docs = text_splitter.split_documents(data)
                
                # Create new vector store
                vectorstore_chroma = Chroma.from_documents(
                    docs, 
                    embeddings, 
                    persist_directory=db_path
                )
                vectorstore_chroma.persist()
                
                st.success("URLs processed successfully! ✅")
                return True
            
        except Exception as e:
            st.error(f"Error processing content: {str(e)}")
            return False

# Function to display processed articles
def show_processed_articles():
    if st.session_state.processed_urls:
        st.subheader("📚 Processed Content")
        articles_df = pd.DataFrame(st.session_state.processed_urls)
        
        # Display as expandable cards
        for idx, article in articles_df.iterrows():
            with st.expander(f"{idx+1}. {article['title']}"):
                if article['url'] != "local_file":
                    st.markdown(f"**URL:** [{article['url']}]({article['url']})")
                st.markdown(f"**Description:** {article['description']}")
                st.markdown(f"**Processed at:** {article['timestamp']}")
                
                # Add delete button
                if st.button(f"Remove {idx+1}", key=f"remove_{idx}"):
                    st.session_state.processed_urls.pop(idx)
                    st.rerun()
        
        # Display as a table view option
        if st.checkbox("Show table view"):
            st.dataframe(articles_df[['title', 'url']])

# Function to generate summary report
def generate_summary_report():
    if os.path.exists(db_path):
        vectorstore = Chroma(persist_directory=db_path, embedding_function=embeddings)
        
        try:
            # Get all documents
            docs = vectorstore.similarity_search("", k=5)
            
            if not docs:
                return "No content available to generate report."
                
            combined_text = "\n\n".join([doc.page_content for doc in docs])
            
            summary_prompt = f"""
            Analyze the following text content and provide a comprehensive summary report:
            
            Content:
            {combined_text}
            
            Please provide:
            1. Key themes and topics covered
            2. Notable facts or statistics mentioned
            3. Overall sentiment analysis
            
            Format your response with clear headings for each section.
            """
            
            with st.spinner("Generating comprehensive report..."):
                report = generate_gemini_response(summary_prompt)
                return report
        except Exception as e:
            return f"Error generating report: {str(e)}"
    else:
        return "No processed content available to generate report."

# Function to visualize topics
def visualize_topics():
    if os.path.exists(db_path):
        vectorstore = Chroma(persist_directory=db_path, embedding_function=embeddings)
        try:
            docs = vectorstore.similarity_search("", k=5)
            
            if not docs:
                st.warning("No content available for analysis")
                return
                
            words = " ".join([doc.page_content for doc in docs]).split()
            word_freq = pd.Series(words).value_counts().reset_index()
            word_freq.columns = ['word', 'count']
            word_freq = word_freq[~word_freq['word'].str.len().lt(4)]
            word_freq = word_freq[~word_freq['word'].str.contains(r'[^a-zA-Z]')]
            word_freq = word_freq.head(10)  # Show top 10 words
            
            if not word_freq.empty:
                fig = px.bar(word_freq, x='word', y='count', title="Frequent Terms")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.warning("Not enough meaningful words to analyze")
        except Exception as e:
            st.error(f"Error in topic analysis: {str(e)}")

# Main execution flow
if process_url_clicked:
    process_urls(urls, uploaded_file if url_input_method == "File Upload" else None)

# Display processed articles in main area
show_processed_articles()

# Query input and processing
query = st.chat_input("Ask a question about the content...")
if query:
    if os.path.exists(db_path):
        vectorstore = Chroma(persist_directory=db_path, embedding_function=embeddings)
        
        # Enhanced similarity search with score threshold
        retrieved_docs = vectorstore.similarity_search_with_score(query, k=max_results)
        retrieved_docs = [doc for doc, score in retrieved_docs if score > 0.5]  # Filter by score
        
        if retrieved_docs:
            context = "\n\n".join([f"Source: {doc.metadata.get('source', 'Unknown')}\nContent: {doc.page_content}" 
                                 for doc in retrieved_docs])
            
            # Add conversation to history
            st.session_state.conversation_history.append({
                "question": query,
                "context": context,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            
            # Generate response with conversation history
            chat_history = st.session_state.memory.load_memory_variables({})['chat_history']
            history_context = "\n".join([f"{msg.content}" for msg in chat_history[-4:]])  # Last 4 messages
            
            with st.spinner("Analyzing content and generating response..."):
                final_prompt = f"""
                Previous conversation context:
                {history_context}
                
                New question: {query}
                
                Relevant content excerpts:
                {context}
                
                Please provide a detailed answer citing sources where appropriate.
                """
                
                response = generate_gemini_response(final_prompt)
                
                # Display response in chat format
                with st.chat_message("user"):
                    st.write(query)
                
                with st.chat_message("assistant"):
                    st.write(response)
                    
                    # Show sources
                    with st.expander("View Sources"):
                        for doc in retrieved_docs:
                            source = doc.metadata.get("source", "Unknown source")
                            st.write(f"- {source}")
        else:
            st.warning("No relevant information found in the content to answer this question.")
    else:
        st.warning("Please process some content first before asking questions.")

# Additional features in tabs
tab1, tab2, tab3 = st.tabs(["📊 Summary Report", "🔍 Topic Analysis", "⚡ Quick Actions"])

with tab1:
    st.subheader("Comprehensive Summary Report")
    if st.button("Generate Report"):
        report = generate_summary_report()
        st.markdown(report)
        
        # Add download option
        st.download_button(
            label="Download Report as Markdown",
            data=report,
            file_name="content_analysis_report.md",
            mime="text/markdown"
        )

with tab2:
    st.subheader("Topic Visualization")
    visualize_topics()

with tab3:
    st.subheader("Quick Actions")
    
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("Clear Conversation History"):
            st.session_state.conversation_history = []
            st.session_state.memory.clear()
            st.success("Conversation history cleared!")
            time.sleep(1)
            st.rerun()
            
        if st.button("Export Chat History"):
            if st.session_state.conversation_history:
                chat_json = json.dumps(st.session_state.conversation_history, indent=2)
                st.download_button(
                    label="Download Chat History",
                    data=chat_json,
                    file_name="content_chat_history.json",
                    mime="application/json"
                )
            else:
                st.warning("No conversation history to export")
    
    with col2:
        if st.button("Reset Vector Database"):
            try:
                if os.path.exists(db_path):
                    # Close any existing Chroma client connections first
                    if 'vectorstore' in globals():
                        del globals()['vectorstore']
                    
                    # Manual cleanup with retries for Windows file locking issues
                    max_retries = 3
                    retry_delay = 1  # seconds
                    
                    for attempt in range(max_retries):
                        try:
                            shutil.rmtree(db_path, ignore_errors=False)
                            st.session_state.processed_urls = []
                            st.success("Vector database reset successfully!")
                            time.sleep(1)
                            st.rerun()
                            break
                        except PermissionError as e:
                            if attempt == max_retries - 1:
                                st.error(f"Failed to reset database after {max_retries} attempts. Please restart the app.")
                            else:
                                time.sleep(retry_delay)
                                continue
            except Exception as e:
                st.error(f"Error resetting database: {str(e)}")
        
        if st.button("Show API Usage"):
            st.info(f"""
            Current configuration:
            - Model: Gemini 1.5 Pro
            - Embeddings: HuggingFace MiniLM
            - Chunk Size: {chunk_size}
            - Max Results: {max_results}
            - Session ID: {st.session_state.session_id}
            """)

# Footer section
st.markdown("---")
footer = """
<style>
.footer {
    position: relative;
    left: 0;
    bottom: 0;
    width: 100%;
    background-color: transparent;
    color: gray;
    text-align: center;
    padding: 10px;
    margin-top: 50px;
}
</style>
<div class="footer">
    <p>☠️ DEVELOPED BY <strong>PRAVIN && RUTURAJ </strong></p>
</div>
"""
st.markdown(footer, unsafe_allow_html=True)


