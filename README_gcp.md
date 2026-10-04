# Hướng dẫn Thực hành LAB 16: Cloud AI Environment Setup (2.5h) - Phiên bản Google Cloud Platform (GCP)

Chào mừng các bạn đến với Lab 16 phiên bản Google Cloud Platform (GCP). Trong bài thực hành này, chúng ta sẽ thiết lập một môi trường Cloud AI hoàn chỉnh trên GCP bằng cách sử dụng **Terraform** (Infrastructure as Code).

**Luồng chính (bắt buộc) của bài lab:** triển khai hạ tầng bằng Terraform, khởi động một **CPU instance nhỏ** (`e2-medium`), và huấn luyện + inference một mô hình **LightGBM** (gradient boosting) thực tế trên đó — không cần GPU, không cần xin quota, không cần tài khoản Hugging Face.

Ở cuối bài có thêm **Phụ lục (Tùy chọn — bài tập nâng cao)**: nếu bạn muốn thử sức và Project của mình xin được quota GPU, bạn có thể triển khai một mô hình ngôn ngữ lớn (LLM — `google/gemma-4-E2B-it`) lên máy chủ GPU (NVIDIA T4) bằng Docker/vLLM, phục vụ qua Cloud Load Balancing. Phần này **không bắt buộc** để hoàn thành lab.

> Không có tài khoản AWS hoặc GCP? Xem [`README_other_clouds.md`](README_other_clouds.md) để làm lab này trên **Azure** hoặc **Oracle Cloud (OCI — có gói Always Free, chi phí $0)**.

---

## Phần 1: Chuẩn bị tài khoản GCP và thiết lập IAM (Least-Privilege)

Trên GCP, mọi tài nguyên đều thuộc về một **Project**. Bạn cần tạo một Project và cấp quyền vừa đủ (least-privilege) cho một Service Account hoặc tài khoản thực hành để Terraform có thể triển khai hạ tầng.

### Bước 1.1: Tạo GCP Project
1. Đăng nhập vào [Google Cloud Console](https://console.cloud.google.com/).
2. Nhấp vào menu chọn Project ở thanh trên cùng (cạnh logo Google Cloud) -> Chọn **New Project**.
3. Đặt tên Project (ví dụ: `ai-lab-16-gcp`) và nhấp **Create**.
4. **LƯU Ý:** Ghi lại **Project ID** (thường có dạng `ai-lab-16-gcp-123456`). Bạn sẽ cần nó cho Terraform.
5. Chắc chắn rằng bạn đã bật **Billing** (thanh toán) cho Project này để có thể tạo tài nguyên.

### Bước 1.2: Kích hoạt các API cần thiết
Để Terraform có thể tạo tài nguyên (máy ảo, network), bạn cần bật các API tương ứng trên Project. Mở **Cloud Shell** (biểu tượng `>_` trên góc phải) và chạy lệnh:
```bash
gcloud services enable compute.googleapis.com iam.googleapis.com
```

### Bước 1.3: Cấp quyền IAM (Least Privilege)
Nếu bạn tự làm lab trên máy cá nhân bằng tài khoản Google của mình (tài khoản đã tạo Project), bạn mặc định có quyền Owner và đã đủ quyền. Tuy nhiên, theo best practice (hoặc nếu phân quyền cho một user/Service Account khác để Terraform chạy), bạn cần vào **IAM & Admin** -> **IAM** và cấp các Roles sau:
- `Compute Admin` (`roles/compute.admin`): Để tạo Compute Engine (VM, Load Balancer, VPC, Firewall, Cloud NAT).
- `Service Account User` (`roles/iam.serviceAccountUser`): Để gán Service Account cho máy ảo Compute Engine.

> **Về GPU Quota:** Luồng chính của bài lab này **không cần** xin tăng quota GPU. Nếu bạn muốn làm thêm Phụ lục (tùy chọn) ở cuối bài để triển khai LLM trên GPU, quy trình xin quota được hướng dẫn riêng ở đó.

---

## Phần 2: Cài đặt và cấu hình môi trường Local

Trên máy tính cá nhân của bạn, mở Terminal/Command Prompt.

### Bước 2.1: Cài đặt và xác thực Google Cloud SDK (gcloud CLI)
Đảm bảo bạn đã cài đặt [Google Cloud CLI](https://cloud.google.com/sdk/docs/install). Gõ lệnh sau để xác thực tài khoản và chọn Project:
```bash
# Đăng nhập vào GCP
gcloud auth login

# Cấp quyền cho Terraform (Application Default Credentials)
gcloud auth application-default login

# Thiết lập Project ID mặc định
gcloud config set project <PROJECT_ID_CỦA_BẠN>
```

*(Nếu bạn định làm Phụ lục GPU + LLM ở cuối bài, phần đó cần thêm một Hugging Face Token — sẽ được hướng dẫn lấy ngay tại đó, không cần chuẩn bị trước.)*

---

## Phần 3: Triển khai Hạ tầng với Terraform

Kiến trúc trên GCP bao gồm:
- **VPC & Subnets**: Một Private Subnet tại `us-central1`.
- **Cloud NAT & Cloud Router**: Cho phép VM ẩn trong Private Subnet truy cập internet để tải package/dataset.
- **Truy cập SSH qua Identity-Aware Proxy (IAP)**: Không cần chạy một VM Bastion riêng tốn chi phí như AWS — [IAP TCP forwarding](https://cloud.google.com/iap/docs/tcp-forwarding-overview) cho phép SSH an toàn thẳng vào Private Subnet.
- **Compute Node**: Máy ảo `e2-medium` (2 vCPU / 4 GB RAM) nằm hoàn toàn trong Private Subnet. Đây là nơi bạn sẽ cài đặt và chạy LightGBM. Instance này **mặc định là CPU**; hạ tầng đã được viết sẵn để chuyển sang GPU (`n1-standard-4` + 1x `nvidia-tesla-t4`) nếu bạn làm Phụ lục ở cuối bài, thông qua biến `gpu_count`.
- **Cloud Load Balancing**: External HTTP Load Balancer trỏ vào port 8000 của Compute Node. Ở luồng CPU mặc định sẽ chưa có gì lắng nghe cổng 8000 nên **health check sẽ hiển thị "unhealthy" — đây là điều bình thường**, bạn không cần xử lý gì cả trừ khi làm Phụ lục GPU + LLM.
- **VPC Firewall Rules**: Chỉ cho phép dải IP của IAP SSH (cổng 22) và dải IP của Load Balancer Healthcheck (cổng 8000) truy cập vào Compute Node.

### Bước 3.1: Khởi tạo Terraform
Di chuyển vào thư mục code Terraform GCP:
```bash
cd terraform-gcp
terraform init
```

### Bước 3.2: Triển khai (Apply)
Với luồng CPU mặc định, bạn chỉ cần khai báo Project ID:
```bash
export TF_VAR_project_id="<PROJECT_ID_CỦA_BẠN>"
terraform apply
```
Gõ `yes` khi được hỏi. Quá trình triển khai hạ tầng mạng trên GCP thường rất nhanh (chưa tới 5 phút).

*Mẹo: Các bạn hãy bắt đầu bấm giờ (benchmark) từ lúc gõ `yes` ở bước này nhé!*

---

## Phần 4: Kết nối và Huấn luyện mô hình LightGBM trên CPU Node

Khi lệnh `terraform apply` chạy xong, bạn sẽ thấy Outputs:
```text
Outputs:

gpu_node_name = "ai-gpu-node"
gpu_node_zone = "us-central1-a"
iap_ssh_command = "gcloud compute ssh ai-gpu-node --zone=us-central1-a --tunnel-through-iap"
load_balancer_ip = "34.120.x.x"
api_endpoint = "http://34.120.x.x/v1"
```
`gpu_node_name`/`gpu_node_zone` là tên và zone của Compute Node (CPU) bạn vừa tạo — tên biến giữ nguyên từ hạ tầng dùng chung với phần GPU tùy chọn. `load_balancer_ip`/`api_endpoint` chỉ có ý nghĩa nếu bạn làm Phụ lục GPU + LLM ở cuối bài; ở luồng CPU bạn có thể bỏ qua hai giá trị này.

### Bước 4.1: SSH vào Compute Node qua IAP
Dùng đúng giá trị output `iap_ssh_command`, hoặc chạy trực tiếp:
```bash
gcloud compute ssh ai-gpu-node --zone=us-central1-a --tunnel-through-iap --project=<PROJECT_ID_CỦA_BẠN>
```

### Bước 4.2: Kiểm tra môi trường ML
Terraform đã tự động cài sẵn Python, LightGBM, scikit-learn, pandas, numpy và Kaggle CLI cho bạn qua startup script. Đợi khoảng 1-2 phút sau khi instance chạy xong rồi kiểm tra:
```bash
python3 -c "import lightgbm, sklearn, pandas, numpy; print('OK')"
```
Nếu chưa thấy `OK` (do startup script còn đang chạy), xem log cài đặt bằng:
```bash
sudo journalctl -u google-startup-scripts.service -f
```

### Bước 4.3: Tải Dataset từ Kaggle

Chúng ta sẽ dùng **Credit Card Fraud Detection** — bộ dữ liệu chuẩn cho benchmark ML với 284,807 giao dịch thực.

**Lấy Kaggle API Key:**
1. Đăng nhập [kaggle.com](https://www.kaggle.com) -> **Settings** -> **API** -> **Create New Token** -> tải về `kaggle.json`.
2. Copy nội dung vào VM:

```bash
mkdir -p ~/.kaggle
# Tạo file credentials (thay YOUR_USERNAME và YOUR_KEY):
cat > ~/.kaggle/kaggle.json << 'EOF'
{"username": "YOUR_KAGGLE_USERNAME", "key": "YOUR_KAGGLE_API_KEY"}
EOF
chmod 600 ~/.kaggle/kaggle.json

mkdir -p ~/ml-benchmark
kaggle datasets download -d mlg-ulb/creditcardfraud --unzip -p ~/ml-benchmark/
```

### Bước 4.4: Huấn luyện và Inference với LightGBM

Viết một script Python (ví dụ `benchmark.py`) thực hiện:
1. Load dataset và tách tập train/test.
2. Huấn luyện một `LGBMClassifier` (hoặc `lightgbm.train`) để phát hiện gian lận.
3. Đo thời gian load data và thời gian training.
4. Đánh giá model trên tập test: AUC-ROC, Accuracy, F1-Score, Precision, Recall.
5. Đo **inference latency** (dự đoán 1 dòng) và **inference throughput** (dự đoán 1000 dòng).
6. Ghi toàn bộ kết quả ra file `benchmark_result.json`.

Chạy script và điền kết quả vào bảng:

| Metric | Kết quả |
|---|---|
| Thời gian load data | 2.925 s |
| Thời gian training | 3.074 s |
| Best iteration | 2 |
| AUC-ROC | 0.940857 |
| Accuracy | 0.989835 |
| F1-Score | 0.233113 |
| Precision | 0.133942 |
| Recall | 0.897959 |
| Inference latency (1 row) | 1.345 ms |
| Inference throughput (1000 rows) | 586,524.712 rows/s |

---

## Phần 5: Kiểm tra Tài nguyên và Chi phí

Ngay sau khi chạy xong benchmark, hãy kiểm tra và chụp lại các chỉ số sau (không cần đợi 1 giờ):

### 5.1: CPU, RAM, Network usage (trên Compute Node, qua SSH)
```bash
# CPU usage theo thời gian thực (nhấn q để thoát)
top

# RAM usage
free -h

# Network usage (số byte/gói tin đã gửi-nhận qua interface)
ip -s link
```
Bạn cũng có thể xem các chỉ số này trên **Google Cloud Console -> Compute Engine -> VM instances -> chọn Compute Node -> tab Monitoring** (biểu đồ CPU utilization, Network traffic).

### 5.2: Billing / Cost Dashboard
1. Truy cập **Billing** -> **Reports** trên Google Cloud Console.
2. Chọn khoảng thời gian hôm nay để xem chi phí hiện tại theo từng dịch vụ.
3. Chụp màn hình thể hiện các dịch vụ đang phát sinh chi phí (Compute Engine, Cloud NAT, Load Balancing).

**Ước tính chi phí/giờ (us-central1) cho luồng CPU mặc định:**

| Dịch vụ | Loại tài nguyên | Chi phí/giờ |
|---|---|---|
| Compute Engine — Compute Node | `e2-medium` | ~$0.033 |
| Cloud NAT | (xử lý egress traffic) | ~$0.044 + data |
| Cloud Load Balancing | External HTTP LB | ~$0.008 |
| **Tổng ước tính** | | **~$0.09/giờ** |

### 5.3: GPU usage (Tùy chọn)
Chỉ áp dụng nếu bạn đã làm Phụ lục GPU + LLM ở cuối bài. Kiểm tra bằng lệnh `nvidia-smi` trên Compute Node (chi tiết ở Phụ lục).

---

## Phần 6: Tiêu chí nộp bài (Deliverables)

Để hoàn thành Lab 16 trên môi trường GCP, bạn cần nộp các kết quả sau:
1. **Screenshot terminal** chạy `python3 benchmark.py` với toàn bộ output kết quả.
2. **File `benchmark_result.json`** chứa metrics đầy đủ (training time, AUC, inference latency, throughput...).
3. **Screenshot tài nguyên**: `top`/`free -h` (hoặc VM Monitoring tab) thể hiện CPU/RAM/Network usage.
4. **Screenshot GCP Billing Reports** thể hiện các dịch vụ đang phát sinh chi phí (Compute Engine, Cloud NAT).
5. **Mã nguồn:** Nén thư mục `terraform-gcp/` đã chạy thành công.
6. **Báo cáo ngắn** (5-10 dòng): nhận xét về kết quả training time, AUC, inference speed trên CPU.

*(Nếu bạn làm thêm Phụ lục GPU + LLM, có thêm các mục nộp bài riêng — xem cuối Phụ lục.)*

---

## Phần 7: Dọn dẹp tài nguyên (CỰC KỲ QUAN TRỌNG)

Cloud NAT và External IP trên GCP sẽ bị trừ tiền liên tục ngay cả khi dùng CPU instance nhỏ. Ngay sau khi test thành công và chụp màn hình nộp bài, bạn **BẮT BUỘC** phải xóa toàn bộ tài nguyên:

```bash
terraform destroy
```
Gõ `yes` để xác nhận việc xóa. Sau khi xóa xong, bạn có thể đăng nhập lại GCP Console để kiểm tra lần cuối, đảm bảo không còn máy ảo (VM instances) nào đang ở trạng thái `Running`.

---

## Phụ lục (Tùy chọn — Bài tập nâng cao): Triển khai GPU + LLM Inference (vLLM)

> Phần này **không bắt buộc**. Nó chỉ dành cho các bạn muốn thử sức thêm và có Project GCP xin được quota GPU. Việc hoàn thành hay không hoàn thành phần này **không ảnh hưởng** đến việc đạt yêu cầu của Lab 16 (Phần 1-7 ở trên).

Mục tiêu: triển khai mô hình ngôn ngữ lớn (LLM — `google/gemma-4-E2B-it`) lên một máy chủ GPU (NVIDIA T4) nằm an toàn trong Private VPC, cung cấp API truy cập ra bên ngoài qua Cloud Load Balancing, dùng Docker/vLLM.

### A.1: Tăng hạn mức (Quota) cho GPU (Rất quan trọng)
GCP mặc định khóa quota GPU (hạn mức = 0) cho các Project mới để phòng chống lạm dụng đào coin. Bạn cần xin tăng quota để chạy được máy ảo gắn GPU T4.
1. Trên thanh tìm kiếm của GCP Console, gõ **Quotas** và chọn trang **Quotas (IAM & Admin)**.
2. Tại bộ lọc (Filter), tìm kiếm thuộc tính:
   - `Quota: GPUs (all regions)`
   - `Quota: NVIDIA T4 GPUs`
3. Tích chọn vào quota **NVIDIA T4 GPUs** tại region bạn định triển khai (ví dụ: `us-central1`).
4. Nhấp **Edit Quotas** -> Điền số lượng mong muốn là **1** -> Gửi yêu cầu (Submit request).
*Lưu ý: Quá trình GCP xét duyệt tăng Quota có thể mất từ vài phút đến 24 giờ. Nếu bị từ chối hoặc chưa duyệt kịp, bạn hoàn toàn có thể bỏ qua phần Phụ lục này — nó là tùy chọn.*

### A.2: Lấy Hugging Face Token
Mô hình `google/gemma-4-E2B-it` là một mô hình bị giới hạn (gated model).
1. Đăng nhập [Hugging Face](https://huggingface.co/).
2. Vào trang mô hình [google/gemma-4-E2B-it](https://huggingface.co/google/gemma-4-E2B-it) và đồng ý với điều khoản (Accept license).
3. Vào **Settings** -> **Access Tokens** -> Tạo một token (quyền Read) và copy lại.

### A.3: Chuyển hạ tầng sang GPU + vLLM
Hạ tầng Terraform đã hỗ trợ sẵn việc bật GPU thông qua biến `gpu_count` và `machine_type` — bạn không cần sửa code, chỉ cần khai báo biến môi trường:
```bash
cd terraform-gcp
export TF_VAR_project_id="<PROJECT_ID_CỦA_BẠN>"
export TF_VAR_machine_type="n1-standard-4"
export TF_VAR_gpu_count=1
export TF_VAR_hf_token="<DÁN_TOKEN_HUGGING_FACE_CỦA_BẠN_VÀO_ĐÂY>"
terraform apply
```
Gõ `yes` khi được hỏi. Terraform sẽ thay thế Compute Node CPU hiện tại bằng một node GPU (`n1-standard-4` + 1x `nvidia-tesla-t4`, Deep Learning VM Image) chạy Docker/vLLM.

> **Quan trọng:** Nếu bạn đã destroy hạ tầng CPU ở Phần 7, việc apply lại với các biến trên sẽ tạo toàn bộ hạ tầng từ đầu (~5 phút cho phần mạng, cộng thêm vài phút để attach GPU). Nếu hạ tầng CPU vẫn đang chạy, Terraform sẽ chỉ thay thế Compute Node, các phần còn lại (VPC, Cloud NAT, Load Balancer...) được giữ nguyên.

### A.4: Kiểm tra AI Endpoint (Inference)
Sau khi apply xong, GPU Node vẫn đang chạy script tải Docker image (vLLM) và model weights (~vài GB) từ Hugging Face. **Bạn cần đợi thêm khoảng 5-10 phút** để model được nạp hoàn toàn vào VRAM của GPU.

Sử dụng IP của Load Balancer (output `load_balancer_ip`) để thực hiện truy vấn AI:
```bash
curl -X POST http://<THAY_BẰNG_LOAD_BALANCER_IP_CỦA_BẠN>/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "google/gemma-4-E2B-it",
    "messages": [
      {"role": "system", "content": "Bạn là một chuyên gia về Google Cloud."},
      {"role": "user", "content": "Hãy giải thích ngắn gọn Cloud NAT trong Google Cloud là gì?"}
    ],
    "max_tokens": 150
  }'
```
Nếu nhận được câu trả lời từ AI, chúc mừng bạn đã triển khai thành công AI Endpoint trên GCP! Ghi lại tổng thời gian (Cold start time) từ lúc chạy `terraform apply` đến lúc lệnh `curl` thành công.

### A.5: SSH vào GPU Node để debug và kiểm tra GPU usage
```bash
gcloud compute ssh ai-gpu-node --zone=us-central1-a --tunnel-through-iap

# Xem log của Docker (vLLM)
sudo docker logs -f vllm

# Kiểm tra GPU utilization, VRAM usage
nvidia-smi
```

### A.6: Tiêu chí nộp bài (Phụ lục GPU + LLM)
Nếu làm thêm phần này, nộp bổ sung các mục sau:
1. **Screenshot API gọi thành công:** Terminal chứa lệnh `curl` và kết quả trả về từ mô hình Gemma.
2. **Report Cold Start Time:** thời gian triển khai từ lúc khởi tạo đến lúc inference thành công (Mục tiêu: < 15 phút cho GPU T4).
3. **Screenshot `nvidia-smi`** thể hiện GPU usage khi model đang chạy.

### A.7: Dọn dẹp
Dù kết thúc ở CPU hay đã chuyển sang GPU, bước dọn dẹp vẫn là chạy `terraform destroy` (xem Phần 7). Máy chủ chứa GPU (`nvidia-tesla-t4`) và External IP sẽ bị trừ tiền liên tục theo giây/phút — đừng quên destroy ngay sau khi test xong.
