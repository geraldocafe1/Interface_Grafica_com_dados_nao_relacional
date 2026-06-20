import os
import json
import datetime
from datetime import UTC
import io
import zipfile
import uuid
from flask import Flask, request, jsonify, render_template, send_file, redirect, url_for
from flask_cors import CORS
from dotenv import load_dotenv

# Importações oficiais da Azure
from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.cosmos import CosmosClient, PartitionKey

# Carrega as variáveis de ambiente do arquivo .env
dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(dotenv_path)

app = Flask(__name__)
CORS(app)

# --- Configuração do Azure Cosmos DB NoSQL API ---
COSMOS_CONN_STR = os.getenv("COSMOS_CONNECTION_STRING")
COSMOS_ENDPOINT = os.getenv("COSMOS_ENDPOINT", "https://mongodbinterface.documents.azure.com:443/")
COSMOS_KEY = os.getenv("COSMOS_KEY")

cosmos_client = None
database = None
colecao = None

# Trata o caso de colarem a chave primária diretamente em COSMOS_CONNECTION_STRING
if COSMOS_CONN_STR:
    if "AccountEndpoint=" in COSMOS_CONN_STR:
        try:
            cosmos_client = CosmosClient.from_connection_string(COSMOS_CONN_STR)
            print("[OK] CosmosClient instanciado a partir de Connection String.")
        except Exception as e:
            print(f"[AVISO] Erro ao instanciar Cosmos DB a partir de Connection String: {e}")
    else:
        COSMOS_KEY = COSMOS_CONN_STR

if not cosmos_client and COSMOS_ENDPOINT and COSMOS_KEY:
    try:
        cosmos_client = CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY)
        print("[OK] CosmosClient instanciado a partir de Endpoint e Key.")
    except Exception as e:
        print(f"[AVISO] Erro ao instanciar Cosmos DB a partir de Endpoint e Key: {e}")

if cosmos_client:
    try:
        # Cria banco de dados se não existir
        database = cosmos_client.create_database_if_not_exists(id="meu_banco_nosql")
        # Cria container se não existir (usando /id como partition key)
        colecao = database.create_container_if_not_exists(
            id="minha_colecao",
            partition_key=PartitionKey(path="/id")
        )
        print("[OK] Banco NoSQL e container do Cosmos DB carregados com sucesso.")
    except Exception as e:
        print(f"[AVISO] Falha ao verificar banco/container no Cosmos DB: {e}. Usando fallback local.")
        colecao = None

# --- Configuração do Azure Blob Storage ---
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
AZURE_CONTAINER_NAME = os.getenv("AZURE_CONTAINER_NAME", "uploads")

blob_service_client = None
container_client = None

if AZURE_STORAGE_CONNECTION_STRING and "sua_connection_string" not in AZURE_STORAGE_CONNECTION_STRING:
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(AZURE_CONTAINER_NAME)
        try:
            # Tenta criar como privado para passar pelas politicas de seguranca restrictas do Azure
            container_client.create_container()
            print(f"[OK] Conteiner Azure Blob '{AZURE_CONTAINER_NAME}' verificado/criado com sucesso.")
        except Exception as e:
            # O contêiner pode já existir ou a criação falhou silenciosamente
            if "ContainerAlreadyExists" not in str(e):
                print(f"[AVISO] Falha ao criar container '{AZURE_CONTAINER_NAME}' na Azure (pode ja existir): {e}")
    except Exception as e:
        print(f"[AVISO] Erro ao inicializar Azure Blob Storage Client: {e}")
else:
    print("[AVISO] AZURE_STORAGE_CONNECTION_STRING nao configurada. Uploads de arquivos serao locais.")

# --- Configuração do Upload Local Fallback ---
LOCAL_UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")

def salvar_arquivo_local(arquivo, filename):
    os.makedirs(LOCAL_UPLOAD_FOLDER, exist_ok=True)
    filepath = os.path.join(LOCAL_UPLOAD_FOLDER, filename)
    arquivo.save(filepath)
    return f"/static/uploads/{filename}"

def deletar_arquivo_local(filename):
    if not filename:
        return
    filepath = os.path.join(LOCAL_UPLOAD_FOLDER, filename)
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
            print(f"[OK] Arquivo local '{filename}' deletado com sucesso.")
        except Exception as e:
            print(f"[AVISO] Erro ao deletar arquivo local '{filename}': {e}")

def obter_tipo_midia(filename, mimetype):
    if not filename:
        return "Nenhum"
    ext = filename.split(".")[-1].lower()
    if mimetype:
        if mimetype.startswith("image/"):
            return "Imagem"
        if mimetype.startswith("video/"):
            return "Vídeo"
        if mimetype.startswith("audio/"):
            return "Áudio"
            
    imagens = ["jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "tiff"]
    videos = ["mp4", "avi", "mkv", "mov", "webm", "flv", "wmv", "mpeg", "3gp"]
    audios = ["mp3", "wav", "ogg", "m4a", "flac", "aac", "wma"]
    documentos = ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf", "odt", "ods", "odp", "csv"]
    
    if ext in imagens:
        return "Imagem"
    elif ext in videos:
        return "Vídeo"
    elif ext in audios:
        return "Áudio"
    elif ext in documentos:
        return "Documento"
    return "Outro"

# --- Mecanismo de Fallback para Banco Local JSON ---
LOCAL_DB_FILE = "local_database.json"

def read_local_db():
    if not os.path.exists(LOCAL_DB_FILE):
        return []
    try:
        with open(LOCAL_DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def write_local_db(data):
    try:
        with open(LOCAL_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"[AVISO] Erro ao gravar banco local JSON: {e}")


@app.route("/", methods=["GET"])
def index():
    filtro_nome = request.args.get("nome", "").strip()
    filtro_tag = request.args.get("tag", "").strip()
    filtro_tipo = request.args.get("tipo", "").strip()
    
    documentos_raw = []
    usar_local = True

    if colecao:
        try:
            # Construção de Query Dinâmica NoSQL SQL-like
            query_str = "SELECT * FROM c WHERE 1=1"
            parameters = []
            
            if filtro_nome:
                query_str += " AND CONTAINS(c.nome, @nome, true)"
                parameters.append({"name": "@nome", "value": filtro_nome})
            if filtro_tag:
                query_str += " AND (CONTAINS(c.nome_arquivo, @tag, true) OR IS_DEFINED(c.tags[@tag]))"
                parameters.append({"name": "@tag", "value": filtro_tag})
            if filtro_tipo:
                query_str += " AND c.tipo_arquivo = @tipo"
                parameters.append({"name": "@tipo", "value": filtro_tipo})
                
            query_str += " ORDER BY c.data_envio DESC"
            
            documentos_raw = list(colecao.query_items(
                query=query_str,
                parameters=parameters,
                enable_cross_partition_query=True
            ))
            usar_local = False
        except Exception as e:
            print(f"[AVISO] Erro na consulta do Cosmos DB, usando fallback local: {e}")
            usar_local = True

    if usar_local:
        documentos_raw = read_local_db()
        if filtro_nome:
            documentos_raw = [d for d in documentos_raw if filtro_nome.lower() in d.get("nome", "").lower()]
        if filtro_tag:
            documentos_raw = [
                d for d in documentos_raw 
                if (d.get("nome_arquivo") and filtro_tag.lower() in d["nome_arquivo"].lower()) or (filtro_tag in d.get("tags", {}))
            ]
        if filtro_tipo:
            documentos_raw = [d for d in documentos_raw if d.get("tipo_arquivo") == filtro_tipo]
        documentos_raw.sort(key=lambda x: x.get("data_envio", ""), reverse=True)
    
    documentos = []
    total_idade = 0
    contagem_idade = 0
    total_tamanho = 0
    formatos = {}

    for doc in documentos_raw:
        # Cosmos DB usa 'id' como chave primaria, mapeamos para '_id' para compatibilidade com o template
        doc["_id"] = doc.get("id")
        
        if doc.get("idade"):
            try:
                total_idade += int(doc["idade"])
                contagem_idade += 1
            except ValueError:
                pass
            
        if doc.get("nome_arquivo"):
            ext = doc["nome_arquivo"].split(".")[-1].upper()
            formatos[ext] = formatos.get(ext, 0) + 1

        if doc.get("tamanho_arquivo"):
            try:
                total_tamanho += int(doc["tamanho_arquivo"])
            except (ValueError, TypeError):
                pass
        elif doc.get("blob_name"):
            # Registros antigos sem tamanho_arquivo: busca tamanho real do blob
            blob_name = doc["blob_name"]
            tamanho_real = 0
            if container_client and not blob_name.startswith("local_"):
                try:
                    blob_client = container_client.get_blob_client(blob_name)
                    props = blob_client.get_blob_properties()
                    tamanho_real = props.size
                    # Atualiza o documento no Cosmos DB para evitar re-consulta futura
                    doc["tamanho_arquivo"] = tamanho_real
                    if colecao:
                        try:
                            colecao.upsert_item(body=doc)
                        except Exception:
                            pass
                except Exception:
                    pass
            else:
                local_path = os.path.join(LOCAL_UPLOAD_FOLDER, blob_name)
                if os.path.exists(local_path):
                    tamanho_real = os.path.getsize(local_path)
                    doc["tamanho_arquivo"] = tamanho_real
                    if not colecao:
                        pass  # será salvo na próxima gravação local
            total_tamanho += tamanho_real
            
        documentos.append(doc)

    media_idade = round(total_idade / contagem_idade, 1) if contagem_idade > 0 else 0
    formato_comum = max(formatos, key=formatos.get) if formatos else "Nenhum"

    def formatar_tamanho(bytes_sz):
        if not bytes_sz:
            return "0 Bytes"
        for unit in ['Bytes', 'KB', 'MB', 'GB']:
            if bytes_sz < 1024:
                return f"{bytes_sz:.1f} {unit}"
            bytes_sz /= 1024
        return f"{bytes_sz:.1f} TB"

    stats = {
        "total": len(documentos),
        "media_idade": media_idade,
        "formato_comum": formato_comum,
        "total_tamanho": formatar_tamanho(total_tamanho),
        "cosmos_status": colecao is not None,
        "storage_status": container_client is not None
    }

    return render_template("index.html", documentos=documentos, filtro_nome=filtro_nome, filtro_tag=filtro_tag, filtro_tipo=filtro_tipo, stats=stats)


@app.route("/upload", methods=["POST"])
def upload():
    try:
        nome = request.form.get("nome")
        idade = request.form.get("idade")
        arquivo = request.files.get("arquivo")
        
        tags_brutas = request.form.get("tags_dinamicas", "").strip()
        tags_mapeadas = {}
        if tags_brutas:
            try:
                tags_mapeadas = json.loads(tags_brutas)
            except json.JSONDecodeError:
                for item in tags_brutas.split(","):
                    if ":" in item:
                        chave, valor = item.split(":", 1)
                        tags_mapeadas[chave.strip()] = valor.strip()

        doc_id = str(uuid.uuid4())
        doc = {
            "id": doc_id,  # Chave primaria obrigatoria no Cosmos DB NoSQL
            "nome": nome,
            "idade": int(idade) if idade else None,
            "data_envio": datetime.datetime.now(UTC).isoformat(),
            "tags": tags_mapeadas,
            "tipo_arquivo": "Nenhum"
        }

        if arquivo and arquivo.filename != '':
            # Extrair tamanho
            arquivo.stream.seek(0, os.SEEK_END)
            tamanho = arquivo.stream.tell()
            arquivo.stream.seek(0)
            
            mimetype = arquivo.content_type or 'application/octet-stream'
            tipo_arquivo = obter_tipo_midia(arquivo.filename, mimetype)
            
            doc["tamanho_arquivo"] = tamanho
            doc["mimetype"] = mimetype
            doc["tipo_arquivo"] = tipo_arquivo
            doc["nome_arquivo"] = arquivo.filename
            
            blob_name = f"{uuid.uuid4().hex}_{arquivo.filename}"
            doc["blob_name"] = blob_name

            if container_client:
                content_settings = ContentSettings(content_type=mimetype)
                try:
                    blob_client = container_client.get_blob_client(blob_name)
                    blob_client.upload_blob(arquivo.stream, overwrite=True, content_settings=content_settings)
                    doc["url_arquivo"] = blob_client.url
                except Exception as e:
                    # Se o container nao existir, tenta criar e reenviar
                    if "ContainerNotFound" in str(e) or "does not exist" in str(e):
                        try:
                            container_client.create_container()
                            arquivo.stream.seek(0)
                            blob_client.upload_blob(arquivo.stream, overwrite=True, content_settings=content_settings)
                            doc["url_arquivo"] = blob_client.url
                        except Exception as inner_e:
                            raise inner_e
                    else:
                        raise e
            else:
                local_url = salvar_arquivo_local(arquivo, blob_name)
                doc["url_arquivo"] = local_url

        if colecao:
            colecao.create_item(body=doc)
        else:
            db_data = read_local_db()
            db_data.append(doc)
            write_local_db(db_data)

        return redirect(url_for("index"))
    except Exception as e:
        return f"Erro no upload: {e}", 500


@app.route("/api/documento/<doc_id>", methods=["GET"])
def obter_documento(doc_id):
    try:
        doc = None
        if colecao:
            try:
                doc = colecao.read_item(item=doc_id, partition_key=doc_id)
            except Exception:
                doc = None

        if not doc:
            db_data = read_local_db()
            for item in db_data:
                if item.get("id") == doc_id:
                    doc = item
                    break

        if not doc:
            return jsonify({"erro": "Documento nao encontrado"}), 404
        
        doc["_id"] = doc.get("id")
        return jsonify(doc)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/editar/<doc_id>", methods=["POST"])
def editar(doc_id):
    try:
        nome = request.form.get("nome")
        idade = request.form.get("idade")
        arquivo = request.files.get("arquivo")
        remover_arquivo = request.form.get("remover_arquivo") == "true"
        
        tags_brutas = request.form.get("tags_dinamicas", "").strip()
        tags_mapeadas = {}
        if tags_brutas:
            try:
                tags_mapeadas = json.loads(tags_brutas)
            except json.JSONDecodeError:
                for item in tags_brutas.split(","):
                    if ":" in item:
                        chave, valor = item.split(":", 1)
                        tags_mapeadas[chave.strip()] = valor.strip()

        # Encontra documento existente
        doc_existente = None
        if colecao:
            try:
                doc_existente = colecao.read_item(item=doc_id, partition_key=doc_id)
            except Exception:
                pass
        if not doc_existente:
            db_data = read_local_db()
            for item in db_data:
                if item.get("id") == doc_id:
                    doc_existente = item
                    break
        
        if not doc_existente:
            return "Documento nao encontrado", 404

        doc_existente["nome"] = nome
        doc_existente["idade"] = int(idade) if idade else None
        doc_existente["tags"] = tags_mapeadas

        # Gerencia remocao ou substituicao de arquivo
        if remover_arquivo or (arquivo and arquivo.filename != ''):
            blob_name_antigo = doc_existente.get("blob_name")
            if blob_name_antigo:
                if container_client and not blob_name_antigo.startswith("local_"):
                    try:
                        blob_client = container_client.get_blob_client(blob_name_antigo)
                        blob_client.delete_blob()
                    except Exception:
                        pass
                else:
                    deletar_arquivo_local(blob_name_antigo)
            
            doc_existente.pop("url_arquivo", None)
            doc_existente.pop("blob_name", None)
            doc_existente.pop("nome_arquivo", None)
            doc_existente.pop("tamanho_arquivo", None)
            doc_existente.pop("mimetype", None)
            doc_existente["tipo_arquivo"] = "Nenhum"

        # Insere novo arquivo caso enviado
        if arquivo and arquivo.filename != '':
            # Extrair tamanho
            arquivo.stream.seek(0, os.SEEK_END)
            tamanho = arquivo.stream.tell()
            arquivo.stream.seek(0)
            
            mimetype = arquivo.content_type or 'application/octet-stream'
            tipo_arquivo = obter_tipo_midia(arquivo.filename, mimetype)
            
            doc_existente["tamanho_arquivo"] = tamanho
            doc_existente["mimetype"] = mimetype
            doc_existente["tipo_arquivo"] = tipo_arquivo
            doc_existente["nome_arquivo"] = arquivo.filename
            
            blob_name = f"{uuid.uuid4().hex}_{arquivo.filename}"
            doc_existente["blob_name"] = blob_name

            if container_client:
                content_settings = ContentSettings(content_type=mimetype)
                try:
                    blob_client = container_client.get_blob_client(blob_name)
                    blob_client.upload_blob(arquivo.stream, overwrite=True, content_settings=content_settings)
                    doc_existente["url_arquivo"] = blob_client.url
                except Exception as e:
                    if "ContainerNotFound" in str(e) or "does not exist" in str(e):
                        try:
                            container_client.create_container()
                            arquivo.stream.seek(0)
                            blob_client.upload_blob(arquivo.stream, overwrite=True, content_settings=content_settings)
                            doc_existente["url_arquivo"] = blob_client.url
                        except Exception as inner_e:
                            raise inner_e
                    else:
                        raise e
            else:
                local_url = salvar_arquivo_local(arquivo, blob_name)
                doc_existente["url_arquivo"] = local_url

        if colecao:
            colecao.upsert_item(body=doc_existente)
        else:
            db_data = read_local_db()
            for idx, item in enumerate(db_data):
                if item.get("id") == doc_id:
                    db_data[idx] = doc_existente
                    break
            write_local_db(db_data)

        return redirect(url_for("index"))
    except Exception as e:
        return f"Erro ao editar: {e}", 500


@app.route("/download/<doc_id>", methods=["GET"])
def download(doc_id):
    try:
        doc = None
        if colecao:
            try:
                doc = colecao.read_item(item=doc_id, partition_key=doc_id)
            except Exception:
                pass
        if not doc:
            db_data = read_local_db()
            for item in db_data:
                if item.get("id") == doc_id:
                    doc = item
                    break

        if not doc or "blob_name" not in doc:
            return "Arquivo nao encontrado para este registro", 404
        
        blob_name = doc["blob_name"]
        
        if container_client and not blob_name.startswith("local_"):
            try:
                blob_client = container_client.get_blob_client(blob_name)
                blob_data = blob_client.download_blob()
                return send_file(
                    io.BytesIO(blob_data.readall()),
                    mimetype=doc.get("mimetype") or blob_data.properties.content_settings.content_type or 'application/octet-stream',
                    download_name=doc.get("nome_arquivo", blob_name),
                    as_attachment=True
                )
            except Exception:
                pass

        # Servir arquivo local
        local_path = os.path.join(LOCAL_UPLOAD_FOLDER, blob_name)
        if os.path.exists(local_path):
            return send_file(
                local_path,
                mimetype=doc.get("mimetype") or 'application/octet-stream',
                download_name=doc.get("nome_arquivo", blob_name),
                as_attachment=True
            )

        if "url_arquivo" in doc and doc["url_arquivo"] != "#":
            return redirect(doc["url_arquivo"])
            
        return "Arquivo nao disponivel", 404
    except Exception as e:
        return f"Erro ao baixar arquivo: {e}", 500


@app.route("/view/<doc_id>")
def view_file(doc_id):
    try:
        doc = None
        if colecao:
            try:
                doc = colecao.read_item(item=doc_id, partition_key=doc_id)
            except Exception:
                pass
        if not doc:
            db_data = read_local_db()
            for item in db_data:
                if item.get("id") == doc_id:
                    doc = item
                    break

        if not doc or "blob_name" not in doc:
            return "Arquivo nao encontrado para este registro", 404
        
        blob_name = doc["blob_name"]
        
        if container_client and not blob_name.startswith("local_"):
            try:
                blob_client = container_client.get_blob_client(blob_name)
                blob_data = blob_client.download_blob()
                return send_file(
                    io.BytesIO(blob_data.readall()),
                    mimetype=doc.get("mimetype") or blob_data.properties.content_settings.content_type or 'application/octet-stream',
                    as_attachment=False
                )
            except Exception:
                pass

        # Servir arquivo local
        local_path = os.path.join(LOCAL_UPLOAD_FOLDER, blob_name)
        if os.path.exists(local_path):
            return send_file(
                local_path,
                mimetype=doc.get("mimetype") or 'application/octet-stream',
                as_attachment=False
            )

        if "url_arquivo" in doc and doc["url_arquivo"] != "#":
            return redirect(doc["url_arquivo"])

        return "Arquivo nao disponivel", 404
    except Exception as e:
        return f"Arquivo nao encontrado: {e}", 404


@app.route("/deletar/<doc_id>", methods=["GET"])
def deletar(doc_id):
    try:
        doc = None
        if colecao:
            try:
                doc = colecao.read_item(item=doc_id, partition_key=doc_id)
            except Exception:
                pass
        if not doc:
            db_data = read_local_db()
            for item in db_data:
                if item.get("id") == doc_id:
                    doc = item
                    break

        if doc:
            blob_name = doc.get("blob_name")
            if blob_name:
                if container_client and not blob_name.startswith("local_"):
                    try:
                        blob_client = container_client.get_blob_client(blob_name)
                        blob_client.delete_blob()
                    except Exception:
                        pass
                else:
                    deletar_arquivo_local(blob_name)
            
            if colecao:
                try:
                    colecao.delete_item(item=doc_id, partition_key=doc_id)
                except Exception as e:
                    print(f"Erro ao deletar do Cosmos DB: {e}")
            else:
                db_data = read_local_db()
                db_data = [item for item in db_data if item.get("id") != doc_id]
                write_local_db(db_data)

        return redirect(url_for("index"))
    except Exception as e:
        return f"Erro ao deletar: {e}", 500


@app.route("/exportar_zip", methods=["GET"])
def exportar_zip():
    try:
        documentos = []
        if colecao:
            documentos = list(colecao.query_items(
                query="SELECT * FROM c WHERE IS_DEFINED(c.blob_name)",
                enable_cross_partition_query=True
            ))
        else:
            documentos = [d for d in read_local_db() if "blob_name" in d]

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
            for doc in documentos:
                nome_arquivo = doc.get("nome_arquivo")
                blob_name = doc.get("blob_name")
                if not nome_arquivo or not blob_name:
                    continue
                
                salvou = False
                if container_client and not blob_name.startswith("local_"):
                    try:
                        blob_client = container_client.get_blob_client(blob_name)
                        blob_data = blob_client.download_blob().readall()
                        zipf.writestr(nome_arquivo, blob_data)
                        salvou = True
                    except Exception:
                        pass
                
                if not salvou:
                    local_path = os.path.join(LOCAL_UPLOAD_FOLDER, blob_name)
                    if os.path.exists(local_path):
                        try:
                            with open(local_path, "rb") as lf:
                                zipf.writestr(nome_arquivo, lf.read())
                        except Exception:
                            pass
                    
        zip_buffer.seek(0)
        return send_file(zip_buffer, download_name="arquivos_enviados.zip", as_attachment=True)
    except Exception as e:
        return f"Erro ao exportar zip: {e}", 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)