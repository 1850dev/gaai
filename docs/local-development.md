# ローカル開発

会話エージェントと週次／月次レポートを確認する場合は、[Agent Harnessガイド](agent-harness.md)の3file Compose構成を使います。結果データはDocker共有volume、metadataは既存Firestoreへ保存し、Cloud Storageの新規作成は不要です。`down -v`は結果データを削除するため使わないでください。

Python 3.12とNode.js 24を基準にします。Python 3.10はhash lock済み依存関係の対象外です。CIとContainerもPython 3.12を使用します。

Firebase AuthenticationとGA4 delegated OAuthは別設定です。Firebase Web Appの公開識別子に加え、GA4接続時だけGoogle OAuth Web ClientとCloud KMSを使用します。Service Account Key JSONは作成・配置しないでください。

## 通常のDocker起動（in-memory）

初回の画面・Firebase Login・ID Token検証だけを確認する場合は、通常のComposeを使います。

```bash
docker compose up --build
```

この構成では`ANALYTICS_AI_REPOSITORY_BACKEND=auto`がlocal環境でin-memoryを選びます。Backendを再起動すると登録データは消えます。未登録ユーザーで`access_denied`になるのは、Authorization Guardがfail-closedで動作している結果です。

DockerのWebは `http://localhost:5174`、APIは `http://localhost:8080` です。別プロジェクト用の5173は使用しません。`.env`のCORS許可元とGA4 OAuth完了後の戻り先も5174へ揃えます。Google側に登録するOAuth callbackは引き続き `http://localhost:8080/api/integrations/ga4/callback` です。

ログイン後、画面右上に`UID ...`が表示されれば、そのUIDは`GET /api/me`でBackendがFirebase ID Tokenを検証して返した値です。BootstrapにはこのUIDを使用します。

## dev Firestoreへ安全に接続する

Firestoreを使うときだけ、開発者本人のApplication Default Credentials（ADC）をread-only volumeとしてContainerへ渡します。これはData Access Service AccountのKeyではありません。対象は必ず分離されたdevプロジェクトにしてください。

1. Google Cloud CLIで開発者ADCを作成します。

   ```bash
   gcloud auth application-default login
   ```

   既存ADCで`invalid_grant`が発生した場合も、同じコマンドを再実行して開発者本人として再認証します。TokenやADC JSONの内容をRepositoryへコピーしないでください。

   ADC作成時に別ProjectがQuota Projectとして選ばれた場合は、Analytics AIのdev Projectへ合わせます。

   ```bash
   gcloud auth application-default set-quota-project YOUR_ANALYTICS_AI_DEV_PROJECT_ID
   ```

2. `.env`へADCファイルの絶対パスを追加します。WSLからDocker Desktopを使う例です。

   WSL版Cloud SDKなら通常は`/home/<user>/.config/gcloud/application_default_credentials.json`、Windows版なら`/mnt/c/Users/<user>/AppData/Roaming/gcloud/application_default_credentials.json`です。`gcloud info --format="value(config.paths.global_config_dir)"`で実際の保存先を確認してください。

   ```dotenv
   GOOGLE_APPLICATION_CREDENTIALS_HOST_PATH=/mnt/c/Users/YOUR_WINDOWS_USER/AppData/Roaming/gcloud/application_default_credentials.json
   # WSL版Cloud SDKの例:
   # GOOGLE_APPLICATION_CREDENTIALS_HOST_PATH=/home/YOUR_WSL_USER/.config/gcloud/application_default_credentials.json
   ANALYTICS_AI_LOCAL_UID=1000
   ANALYTICS_AI_LOCAL_GID=1000
   ```

3. Firestore override付きで再起動します。

   ```bash
   docker compose down
   docker compose -f compose.yaml -f compose.firestore.yaml up --build
   ```

`compose.firestore.yaml`は次だけを変更します。

- Repositoryを明示的に`firestore`へ切り替える
- 開発者ADCをContainer内の固定パスへread-only mountする
- private bootstrap manifest用の`.bootstrap`をread-only mountする

通常の`docker compose up`にはCredential mountがないため、誤ってdev Firestoreへ接続しません。dev／stg／prodでin-memory Repositoryを指定すると起動時に拒否されます。本番Cloud Runではvolume mountではなくRuntime Service AccountのADCを使用します。

## 最初のUser／CompanyをBootstrapする

Bootstrapはlocal／dev限定、Analytics Tool無効時限定、create-onlyです。既存Documentの上書きはせず、同じFirestore batchにAudit Eventを作成します。

1. private manifestを作成します。

   ```bash
   mkdir -p .bootstrap
   cp docs/examples/bootstrap.identity.example.json .bootstrap/dev.json
   ```

2. `.bootstrap/dev.json`のUID、email、Company ID、Company名を編集します。`.bootstrap/`はGit管理対象外です。

3. Firestore overrideを指定して実行します。

   ```bash
   docker compose -f compose.yaml -f compose.firestore.yaml run --rm analysis-api \
     python -m analytics_ai.bootstrap --manifest /bootstrap/dev.json
   ```

4. ブラウザを再読み込みします。User登録後は`access_denied`ではなく、Data Source Setが0件のEmpty Stateになります。

Data Source Set、Grant、Semantic Definitionは[bootstrap.data-source.example.json](examples/bootstrap.data-source.example.json)を別manifestとして投入できます。GA4標準の`USER_OAUTH`だけならData Access Profileは不要です。BigQueryまたは明示的なGA4 `SERVICE_ACCOUNT` modeを使う場合だけProfileを登録します。`stream_id`には`G-...`形式のGoogleタグIDではなく数値のStream IDを指定します。CV未定義の情報サイトでは、例のように`available: false`を明示し、AppのKey Eventを流用しません。BootstrapはData Source Setを有効化できません。接続検証と有効化は後続のAdmin workflowで実施します。

## GA4 User OAuthをローカルで確認する

1. dev Google Cloud Projectの「APIとサービス」でOAuth同意画面を設定し、OAuth 2.0 Web Application Clientを作成します。
2. Authorized redirect URIへ次を完全一致で登録します。

   ```text
   http://localhost:8080/api/integrations/ga4/callback
   ```

3. dev ProjectにCloud KMS Keyを用意し、現在の開発者ADCへ検証用の暗号化／復号権限を付与します。本番Runtime以外へ恒久付与しないでください。
4. Git-ignoredの`.env`へ次を設定します。

   ```dotenv
   ANALYTICS_AI_ENABLE_GA4_USER_OAUTH=true
   ANALYTICS_AI_GA4_OAUTH_CLIENT_ID=...apps.googleusercontent.com
   ANALYTICS_AI_GA4_OAUTH_CLIENT_SECRET=...
   ANALYTICS_AI_GA4_OAUTH_REDIRECT_URI=http://localhost:8080/api/integrations/ga4/callback
   ANALYTICS_AI_GA4_OAUTH_RETURN_URL=http://localhost:5174
   ANALYTICS_AI_GA4_OAUTH_KMS_KEY_NAME=projects/PROJECT/locations/REGION/keyRings/RING/cryptoKeys/KEY
   ANALYTICS_AI_ENABLE_ANALYTICS_TOOLS=false
   ```

5. Firestore override付きComposeを再ビルドし、画面の「Google Analyticsを接続」を実行します。

OAuth Client Secretはlocal `.env`だけの例外です。dev／stg／prod Cloud RunではSecret Manager参照を使い、Terraform state、tfvars、Container imageへ値を保存しません。Refresh TokenはlocalでもCloud KMS暗号文だけをFirestoreへ保存します。

## ローカルプロセスで起動する場合

### Backend

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r services/analysis-api/requirements-dev.lock
python -m pip install --no-deps -e services/analysis-api
uvicorn analytics_ai.api.app:app --app-dir services/analysis-api/src --reload --port 8080
```

### Web

```bash
npm --prefix apps/web ci
npm --prefix apps/web run dev
```

`ANALYTICS_AI_ALLOW_INSECURE_DEV_AUTH=true`を使う開発Verifierは、`ANALYTICS_AI_ENVIRONMENT=local`との組み合わせでしか起動できません。通常のFirebase確認では使用しないでください。

Analytics実行は既定で無効です。Firebase検証、Firestore records、GA4 User OAuth／BigQuery Profile、接続検証、実devプロジェクトの分離Acceptance Testが完了するまで、`ANALYTICS_AI_ENABLE_ANALYTICS_TOOLS=false`を維持します。

ローカルで安全確認済みのGA4 OAuth接続を実分析する場合だけ、Repositoryルートの`.env`で`ANALYTICS_AI_ENABLE_ANALYTICS_TOOLS=true`へ変更し、Backendを再起動します。`ANALYTICS_AI_CACHE_BACKEND=auto`はlocal／testではin-memory cache、dev／stg／prodではFirestore metadata＋Cloud Storage blobへ解決されます。したがって、localのFirestore検証に本番用Cache bucketは不要です。非local環境でmemory cacheは起動時に拒否されます。

登録済みPropertyの接続確認には独立した`ANALYTICS_AI_ENABLE_ADMIN_CONNECTION_CHECKS`を使用します。GA4 USER_OAUTHの接続・Property discoveryは`ANALYTICS_AI_ENABLE_GA4_USER_OAUTH`で制御します。どちらのGateも通常のMessage分析を公開しません。
