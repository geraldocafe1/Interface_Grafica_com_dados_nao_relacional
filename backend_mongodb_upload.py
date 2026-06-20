import os
import json
import datetime
from datetime import UTC
import io
import zipfile
import uuid
from flask import Flask, request, jsonify, render_template, send_file, redirect, url_for
from flask_cors import CORS
from pymongo import MongoClient
from bson import ObjectId
from gridfs import GridFS
from dotenv import load_dotenv
from azure.storage.blob import BlobServiceClient

# Carrega as variáveis de ambiente do arquivo .env
load_dotenv()

app = Flask(__name__)
CORS(app)

# --- Configuração Azure Cosmos DB ---
MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    raise ValueError("[ERRO] Variavel MONGO_URI nao encontrada no arquivo .env.")

client = MongoClient(MONGO_URI)
db = client["meu_banco_nosql"]
colecao = db["minha_colecao"]
fs = GridFS(db)

# --- Configuração Azure Blob Storage ---
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
AZURE_CONTAINER_NAME = os.getenv("AZURE_CONTAINER_NAME", "uploads")

blob_service_client = None
container_client = None

if AZURE_STORAGE_CONNECTION_STRING:
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(AZURE_CONTAINER_NAME)
        # Cria o contêiner se não existir e o configura para leitura pública de blobs
        try:
            container_client.create_container(public_access="blob")
            print(f"[OK] Conteiner Azure Blob '{AZURE_CONTAINER_NAME}' verificado/criado com sucesso.")
        except Exception:
            # O contêiner pode já existir
            pass
    except Exception as e:
        print(f"[AVISO] Erro ao inicializar Azure Blob Storage Client: {e}")
else:
    print("[AVISO] AZURE_STORAGE_CONNECTION_STRING nao encontrada no arquivo .env. Usando GridFS como fallback.")


@app.route("/", methods=["GET"])
def index():
    filtro_nome = request.args.get("nome", "").strip()
    filtro_tag = request.args.get("tag", "").strip()
    
    # Construção da Query Dinâmica NoSQL
    query = {}
    if filtro_nome:
        query["nome"] = {"$regex": filtro_nome, "$options": "i"}
    if filtro_tag:
        # Busca tanto na chave quanto no valor das tags dinâmicas
        query["$or"] = [
            {f"tags.{filtro_tag}": {"$exists": True}},
            {"nome_arquivo": {"$regex": filtro_tag, "$options": "i"}}
        ]

    documentos_raw = list(colecao.find(query).sort("data_envio", -1))
    
    documentos = []
    total_idade = 0
    contagem_idade = 0
    formatos = {}

    for doc in documentos_raw:
        doc["_id"] = str(doc["_id"])
        if "arquivo_id" in doc:
            doc["arquivo_id"] = str(doc["arquivo_id"])
        
        # Dados para os Cards de Estatísticas do Dashboard
        if doc.get("idade"):
            total_idade += doc["idade"]
            contagem_idade += 1
            
        if doc.get("nome_arquivo"):
            ext = doc["nome_arquivo"].split(".")[-1].upper()
            formatos[ext] = formatos.get(ext, 0) + 1
            
        documentos.append(doc)

    # Cálculos das métricas
    media_idade = round(total_idade / contagem_idade, 1) if contagem_idade > 0 else 0
    formato_comum = max(formatos, key=formatos.get) if formatos else "Nenhum"

    stats = {
        "total": len(documentos),
        "media_idade": media_idade,
        "formato_comum": formato_comum
    }

    return render_template("index.html", documentos=documentos, filtro_nome=filtro_nome, filtro_tag=filtro_tag, stats=stats)


@app.route("/upload", methods=["POST"])
def upload():
    try:
        nome = request.form.get("nome")
        idade = request.form.get("idade")
        arquivo = request.files.get("arquivo")
        
        # DEMONSTRAÇÃO NOSQL: Captura chaves e valores dinâmicos do formulário
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

        doc = {
            "nome": nome,
            "idade": int(idade) if idade else None,
            "data_envio": datetime.datetime.now(UTC),
            "tags": tags_mapeadas  # Estrutura de subdocumento aninhado flexível
        }

        if arquivo and arquivo.filename != '':
            if container_client:
                # Gerar nome único para o blob para evitar colisão
                ext = arquivo.filename.split(".")[-1] if "." in arquivo.filename else ""
                blob_name = f"{uuid.uuid4().hex}_{arquivo.filename}"
                blob_client = container_client.get_blob_client(blob_name)
                
                # Fazer o upload do stream para o Azure Blob Storage
                blob_client.upload_blob(arquivo.stream, overwrite=True)
                
                doc["url_arquivo"] = blob_client.url
                doc["blob_name"] = blob_name
                doc["nome_arquivo"] = arquivo.filename
            else:
                # Fallback para GridFS
                file_id = fs.put(arquivo, filename=arquivo.filename, content_type=arquivo.mimetype)
                doc["arquivo_id"] = file_id
                doc["nome_arquivo"] = arquivo.filename

        colecao.insert_one(doc)
        return redirect(url_for("index"))
    except Exception as e:
        return f"Erro no upload: {e}", 500


@app.route("/api/documento/<doc_id>", methods=["GET"])
def obter_documento(doc_id):
    try:
        doc = colecao.find_one({"_id": ObjectId(doc_id)})
        if not doc:
            return jsonify({"erro": "Documento não encontrado"}), 404
        
        # Serialização para representação JSON
        doc["_id"] = str(doc["_id"])
        if "arquivo_id" in doc:
            doc["arquivo_id"] = str(doc["arquivo_id"])
        if "data_envio" in doc:
            doc["data_envio"] = doc["data_envio"].isoformat()
            
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

        # Recupera o documento existente para gerenciar arquivos
        doc_id_obj = ObjectId(doc_id)
        doc_existente = colecao.find_one({"_id": doc_id_obj})
        
        if not doc_existente:
            return "Documento não encontrado", 404

        update_fields = {
            "nome": nome,
            "idade": int(idade) if idade else None,
            "tags": tags_mapeadas
        }

        # Gerencia remoção ou substituição de arquivo
        if remover_arquivo or (arquivo and arquivo.filename != ''):
            if "arquivo_id" in doc_existente:
                try:
                    fs.delete(doc_existente["arquivo_id"])
                except Exception:
                    pass
                colecao.update_one({"_id": doc_id_obj}, {"$unset": {"arquivo_id": "", "nome_arquivo": ""}})

            if "blob_name" in doc_existente and container_client:
                try:
                    blob_client = container_client.get_blob_client(doc_existente["blob_name"])
                    blob_client.delete_blob()
                except Exception:
                    pass
                colecao.update_one({"_id": doc_id_obj}, {"$unset": {"url_arquivo": "", "blob_name": ""}})

            # Remove da query fields que possam persistir
            colecao.update_one({"_id": doc_id_obj}, {"$unset": {"nome_arquivo": ""}})

        # Insere novo arquivo caso enviado
        if arquivo and arquivo.filename != '':
            if container_client:
                blob_name = f"{uuid.uuid4().hex}_{arquivo.filename}"
                blob_client = container_client.get_blob_client(blob_name)
                blob_client.upload_blob(arquivo.stream, overwrite=True)
                update_fields["url_arquivo"] = blob_client.url
                update_fields["blob_name"] = blob_name
                update_fields["nome_arquivo"] = arquivo.filename
            else:
                # Fallback GridFS
                file_id = fs.put(arquivo, filename=arquivo.filename, content_type=arquivo.mimetype)
                update_fields["arquivo_id"] = file_id
                update_fields["nome_arquivo"] = arquivo.filename

        colecao.update_one({"_id": doc_id_obj}, {"$set": update_fields})
        return redirect(url_for("index"))
    except Exception as e:
        return f"Erro ao editar: {e}", 500



@app.route("/download/<doc_id>", methods=["GET"])
def download(doc_id):
    try:
        doc_id_obj = ObjectId(doc_id)
        doc = colecao.find_one({"_id": doc_id_obj})
        if not doc:
            return "Documento não encontrado", 404
        
        # 1. Se estiver no GridFS
        if "arquivo_id" in doc:
            arquivo = fs.get(doc["arquivo_id"])
            return send_file(io.BytesIO(arquivo.read()), download_name=arquivo.filename, as_attachment=True)
        
        # 2. Se estiver no Azure Blob Storage
        elif "blob_name" in doc:
            if container_client:
                blob_client = container_client.get_blob_client(doc["blob_name"])
                blob_data = blob_client.download_blob()
                return send_file(
                    io.BytesIO(blob_data.readall()),
                    mimetype=blob_data.properties.content_settings.content_type or 'application/octet-stream',
                    download_name=doc.get("nome_arquivo", doc["blob_name"]),
                    as_attachment=True
                )
            elif "url_arquivo" in doc:
                return redirect(doc["url_arquivo"])
                
        return "Arquivo não encontrado para este registro", 404
    except Exception as e:
        return "Erro ao baixar arquivo", 500


@app.route("/view/<doc_id>")
def view_file(doc_id):
    try:
        doc_id_obj = ObjectId(doc_id)
        doc = colecao.find_one({"_id": doc_id_obj})
        if not doc:
            return "Documento não encontrado", 404
        
        # 1. Se estiver no GridFS
        if "arquivo_id" in doc:
            grid_out = fs.get(doc["arquivo_id"])
            return send_file(io.BytesIO(grid_out.read()), mimetype=grid_out.content_type, as_attachment=False)
        
        # 2. Se estiver no Azure Blob Storage
        elif "blob_name" in doc:
            if container_client:
                blob_client = container_client.get_blob_client(doc["blob_name"])
                blob_data = blob_client.download_blob()
                return send_file(
                    io.BytesIO(blob_data.readall()),
                    mimetype=blob_data.properties.content_settings.content_type or 'application/octet-stream',
                    as_attachment=False
                )
            elif "url_arquivo" in doc:
                return redirect(doc["url_arquivo"])

        return "Arquivo não encontrado para este registro", 404
    except Exception as e:
        return "Arquivo não encontrado", 404


@app.route("/deletar/<doc_id>", methods=["GET"])
def deletar(doc_id):
    try:
        doc_id_obj = ObjectId(doc_id)
        doc = colecao.find_one({"_id": doc_id_obj})
        
        if doc:
            # Excluir do GridFS se houver
            if "arquivo_id" in doc:
                try:
                    fs.delete(doc["arquivo_id"])
                except Exception:
                    pass
            # Excluir do Azure Blob Storage se houver
            if "blob_name" in doc and container_client:
                try:
                    blob_client = container_client.get_blob_client(doc["blob_name"])
                    blob_client.delete_blob()
                except Exception:
                    pass
            
            colecao.delete_one({"_id": doc_id_obj})
        return redirect(url_for("index"))
    except Exception as e:
        return "Erro ao deletar", 500


@app.route("/exportar_zip", methods=["GET"])
def exportar_zip():
    try:
        documentos = colecao.find({"$or": [{"arquivo_id": {"$exists": True}}, {"url_arquivo": {"$exists": True}}]})
        zip_buffer = io.BytesIO()
        
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
            for doc in documentos:
                nome_arquivo = doc.get("nome_arquivo")
                if not nome_arquivo:
                    continue
                try:
                    if "arquivo_id" in doc:
                        arquivo_gridfs = fs.get(doc["arquivo_id"])
                        zipf.writestr(nome_arquivo, arquivo_gridfs.read())
                    elif "blob_name" in doc and container_client:
                        blob_client = container_client.get_blob_client(doc["blob_name"])
                        blob_data = blob_client.download_blob().readall()
                        zipf.writestr(nome_arquivo, blob_data)
                except Exception:
                    continue
                    
        zip_buffer.seek(0)
        return send_file(zip_buffer, download_name="arquivos_enviados.zip", as_attachment=True)
    except Exception as e:
        return "Erro ao exportar zip", 500


if __name__ == "__main__":
    try:
        colecao.create_index([("data_envio", -1)])
        print("[OK] Indice 'data_envio' verificado/criado com sucesso.")
    except Exception as e:
        print(f"[AVISO] Aviso ao verificar/criar indice: {e}")

    app.run(debug=True, port=5000)