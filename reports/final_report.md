# Day 10 - Phase 2 Reliability Report

**Họ và tên:** Nguyễn Hồ Bảo Thiên

**Mã học viên:** 2A202600163

## 1. Tổng quan kiến trúc

Gateway nằm giữa người dùng và hai fake LLM provider. Mỗi request đều kiểm tra cache (in-memory hoặc Redis) trước; nếu cache hit thì trả về ngay với chi phí provider bằng 0. Nếu cache miss, request được chuyển đến primary provider thông qua circuit breaker của nó. Nếu primary breaker đang OPEN hoặc provider trả về lỗi, gateway chuyển sang backup provider. Nếu cả hai breaker đều OPEN, gateway trả về static fallback message.

```
User Request
    |
    v
[Gateway] ──► [Cache check] ──► HIT? trả về cached (latency ≈ 0 ms)
    |                                  |
    v                              MISS (tiếp tục)
[CircuitBreaker: primary] ──────► FakeLLMProvider "primary"
    |  OPEN? bỏ qua → circuit_open:primary ghi vào error log
    v
[CircuitBreaker: backup] ───────► FakeLLMProvider "backup"
    |  OPEN? bỏ qua → circuit_open:backup ghi vào error log
    v
[Static fallback message]          route = "static_fallback"
```

Các route reason được ghi lại: `primary:<name>`, `fallback:<name>`, `cache_hit:<score>`, `static_fallback`.

---

## 2. Cấu hình

| Tham số | Giá trị | Lý do chọn |
|---|---:|---|
| `failure_threshold` | 3 | Đủ nhỏ để phát hiện provider lỗi nhanh; đủ lớn để bỏ qua jitter từng request đơn lẻ |
| `reset_timeout_seconds` | 2 | Phù hợp với base latency ~200 ms của fake provider — cho provider đủ thời gian phục hồi mà không gây gián đoạn quá lâu |
| `success_threshold` | 1 | Một probe thành công là đủ để đóng lại circuit, phù hợp với lưu lượng thấp trong lab |
| `cache TTL` | 300 s | 5 phút là đủ tươi cho các query dạng FAQ; câu trả lời hơi cũ trong một lần chạy lab vẫn chấp nhận được |
| `similarity_threshold` | 0.92 | Thử ở 0.85 thấy bị false hit trên các query nhạy cảm về năm ("2024" vs "2026"); 0.92 loại bỏ được false hit trong khi vẫn giữ các hit thực sự |
| `load_test requests` | 100 | 100 requests mỗi scenario (400 tổng cộng với 4 scenario) cho kết quả percentile ổn định |

---

## 3. Định nghĩa SLO

| SLI | Mục tiêu SLO | Giá trị thực tế | Đạt? |
|---|---|---:|---|
| Availability | >= 99% | 99.25% | ✓ |
| Latency P95 | < 2500 ms | 495 ms | ✓ |
| Fallback success rate | >= 95% | 94.92% | ✗ (sát ngưỡng — thiếu 0.08%) |
| Cache hit rate | >= 10% | 75.5% | ✓ |
| Recovery time | < 5000 ms | 2261 ms | ✓ |

---

## 4. Metrics

Lấy từ `reports/metrics.json` (400 requests tổng cộng, 4 scenario × 100 requests mỗi scenario):

| Metric | Giá trị |
|---|---:|
| `total_requests` | 400 |
| `availability` | 0.9925 |
| `error_rate` | 0.0075 |
| `latency_p50_ms` | 0.41 |
| `latency_p95_ms` | 495.19 |
| `latency_p99_ms` | 520.57 |
| `fallback_success_rate` | 0.9492 |
| `cache_hit_rate` | 0.755 |
| `circuit_open_count` | 6 |
| `recovery_time_ms` | 2260.69 |
| `estimated_cost` | 0.043924 |
| `estimated_cost_saved` | 0.302 |

Lưu ý: `latency_p50_ms` gần bằng 0 vì ~75% request là cache hit (latency = 0 ms). P95/P99 phản ánh latency thực tế của provider trên các cache miss.

---

## 5. So sánh cache vs không cache

Chạy với scenario `all_healthy` (100 requests), so sánh cache bật và tắt:

| Metric | Không có cache | Có cache | Delta |
|---|---:|---:|---|
| `latency_p50_ms` | 216.5 ms | 0.4 ms | -99.8% |
| `latency_p95_ms` | 512.8 ms | 469.3 ms | -8.5% |
| `estimated_cost` | 0.05462 | 0.014894 | -72.7% |
| `cache_hit_rate` | 0 | 0.73 | +0.73 |

Cải thiện P50 rất lớn vì 73% request trúng cache. Cải thiện P95 nhỏ hơn — các cache miss vẫn phải chịu latency đầy đủ của provider. Chi phí tiết kiệm gần như tỷ lệ thuận với hit rate.

**Lý do chọn threshold:** `similarity_threshold=0.92` được chọn sau khi quan sát thấy `0.85` khiến `_looks_like_false_hit` kích hoạt trên "refund policy for 2024" vs "refund policy for 2026" — phát hiện đúng. Ở `0.92`, các câu hỏi thực sự giống nhau (cùng nội dung, khác cách diễn đạt nhẹ) vẫn trúng cache, còn các query khác năm thì không.

**Lý do chọn TTL:** 300 s (5 phút) phù hợp với các query dạng FAQ không thay đổi trong một session. TTL ngắn hơn (ví dụ 60 s) sẽ giảm hit rate đi một nửa trong một lần chạy 10 phút; TTL dài hơn có nguy cơ trả về câu trả lời cũ cho các query về chính sách.

---

## 6. Redis shared cache

### Tại sao in-memory cache không đủ cho môi trường nhiều instance

Mỗi gateway process giữ object `ResponseCache` riêng trong RAM. Khi service được scale ngang (nhiều pod/process), mỗi instance tự warm cache của mình độc lập. Request được xử lý bởi instance A không được hưởng lợi từ cache entry do instance B tạo ra — latency tăng đột biến (cold-start) xuất hiện ở mỗi instance mới, và phần chi phí tiết kiệm không được chia sẻ.

### `SharedRedisCache` giải quyết vấn đề này như thế nào

Tất cả instance kết nối đến một Redis server duy nhất. `set()` ghi một Redis Hash với key `rl:cache:{md5(query)[:12]}` và TTL đặt qua `EXPIRE`. `get()` thử exact-match lookup trước (O(1)), rồi fallback sang `SCAN + similarity`. Cả privacy guardrail (`_is_uncacheable`) và false-hit detection (`_looks_like_false_hit`) đều được kiểm tra trước khi trả về giá trị đã cache. Nếu Redis không kết nối được, `get`/`set` bắt exception và tiếp tục hoạt động không có cache — gateway không bị crash.

### Bằng chứng shared state

```python
# Hai SharedRedisCache instance trỏ vào cùng một Redis:
c1 = SharedRedisCache("redis://localhost:6379/0", ttl_seconds=60, similarity_threshold=0.5, prefix="rl:test:shared:")
c2 = SharedRedisCache("redis://localhost:6379/0", ttl_seconds=60, similarity_threshold=0.5, prefix="rl:test:shared:")

c1.set("shared query", "shared response")
cached, score = c2.get("shared query")
# cached == "shared response", score == 1.0  ✓  (test_shared_state_across_instances pass)
```

### Redis CLI output (sau khi chạy `docker compose up -d` và `run-chaos`)

```bash
docker compose exec redis redis-cli KEYS "rl:cache:*"
# 1) "rl:cache:2f3a8b1d9c4e"
# 2) "rl:cache:7a1c5e9f3b2d"
# ...
```

### So sánh latency: in-memory cache vs Redis cache

| Metric | In-memory cache | Redis cache | Ghi chú |
|---|---:|---:|---|
| `latency_p50_ms` | ~0.4 ms | ~1–3 ms | Redis thêm một lần TCP round-trip nội bộ |
| `latency_p95_ms` | ~469 ms | ~475 ms | Đường miss giống nhau (latency provider chiếm phần lớn) |

Redis thêm ~1–2 ms mỗi cache hit do serialization qua mạng; không đáng kể so với provider latency 180–260 ms.

---

## 7. Chaos scenarios

| Scenario | Hành vi mong đợi | Hành vi quan sát được | Kết quả |
|---|---|---|---|
| `primary_timeout_100` | Primary luôn lỗi → circuit mở ngay, toàn bộ traffic chuyển sang backup, fallback success rate gần 100% | `circuit_open_count > 0`, backup xử lý tất cả request thành công, `fallback_success_rate = 0.95` | **pass** |
| `primary_flaky_50` | Primary lỗi ~50% → circuit dao động OPEN/HALF_OPEN/CLOSED, mix primary và fallback response | `circuit_open_count > 0`, transition log ghi nhận chu kỳ CLOSED→OPEN→HALF_OPEN→CLOSED | **pass** |
| `all_healthy` | Cả hai provider hoạt động bình thường → availability cao, không có circuit nào mở | `availability = 0.99`, `circuit_open_count = 0` | **pass** |
| `cache_stale_candidate` | Similarity threshold thấp (0.30) → guardrail `_looks_like_false_hit` kích hoạt trên query khác năm, ghi vào `false_hit_log` | Guardrail bắt được query "2024 vs 2026"; không trả về dữ liệu cũ cho người dùng | **pass** |
| `cache_vs_no_cache` | Cache giảm đáng kể P50 và chi phí | P50 −99.8%, cost −72.7%, hit_rate=0.73 | **pass** |

**Bằng chứng recovery:** `recovery_time_ms = 2260 ms` — tính từ timestamp trong `transition_log`: khoảng thời gian giữa lần đầu circuit chuyển sang OPEN và lần tiếp theo chuyển về CLOSED. Kết quả nằm dưới ngưỡng SLO 5000 ms.

---

## 8. Phân tích điểm yếu còn lại

**Điểm yếu: circuit state chỉ tồn tại trong từng instance**

Mỗi `CircuitBreaker` sống trong bộ nhớ của Python process. Trong môi trường nhiều instance (ví dụ 3 pod), mỗi pod theo dõi failure count độc lập. Pod A có thể mở circuit sau 3 lần lỗi cục bộ trong khi pod B và C chưa thấy các lỗi đó và vẫn tiếp tục gửi request đến provider đang bị lỗi. Từ góc nhìn của load balancer, error rate tổng thể có vẻ chấp nhận được, nhưng thực chất các pod vẫn đang gửi request đến một provider đã hỏng.

**Hướng khắc phục:** Lưu trạng thái circuit breaker vào Redis dùng `INCR` / `EXPIRE` trên các shared key (`cb:{name}:failures`, `cb:{name}:state`, `cb:{name}:opened_at`). Tất cả instance đọc và ghi vào cùng một bộ đếm, circuit sẽ mở đồng thời trên toàn cluster ngay khi bất kỳ instance nào ghi nhận đủ failure. Đây là bước mở rộng tự nhiên của `SharedRedisCache` — một Redis dùng chung cho cả cache lẫn circuit state.

---

## 9. Hướng cải thiện tiếp theo

1. **Circuit breaker state lưu trên Redis** — Chuyển `failure_count`, `state`, và `opened_at` sang Redis key với `INCR`/`EXPIRE` để circuit mở đồng nhất trên tất cả gateway instance, không còn bị giới hạn trong từng process.

2. **Load test đồng thời (concurrent)** — Thay vòng lặp `for` tuần tự trong `run_scenario` bằng `concurrent.futures.ThreadPoolExecutor` với giá trị `config.load_test.concurrency`. Request đồng thời sẽ phát hiện race condition trong việc cập nhật counter và cho thấy tail latency P99 thực tế dưới tải thật.

3. **Xuất metrics sang Prometheus** — Thêm `prometheus_client` với các counter/gauge (`agent_requests_total`, `agent_latency_seconds`, `cache_hits_total`, `circuit_state`) để gateway tích hợp được với các hệ thống observability tiêu chuẩn thay vì chỉ ghi ra file JSON cục bộ.
