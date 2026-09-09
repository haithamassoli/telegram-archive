# خطة تنحيف آثار التفريغ

**الحالة:** ✅ مُنفَّذة · **المرجع:** تقرير `docs/report-transcripts.html` · **تمسّ:** `src/archive/transcribe.py` و`cli.py` و`tests/test_m2.py`

> **تعديل بعد المراجعة:** `speech_spans` نُقلت من «يبقى» إلى «يُحذف» بقرار صريح. كان تبريري لإبقائها
> أنها تُعيد بناء `words` — وهو تبرير دائريّ، لأن `words` نفسها لا نريدها. وهي تسقط في اختبار
> «هل تذكرها الخطة؟» تمامًا كما سقطت `words` و`cues`. النتيجة: **22.2 كيلوبايت** بدل 29.7.

الهدف: أن يحمل كل أثر تفريغ ما لا يمكن إعادة توليده، ولا شيء غيره. النتيجة المقيسة:
**‎518.7‎ كيلوبايت للملف تصير ‎22.2‎** — أي ‎5.2‎ غيغابايت تصير ‎223‎ ميغابايت، بانخفاض **‎95.7٪‎**، وبلا مساسٍ بـ`segments` ولا بحقلٍ واحدٍ تقرأه `validate_artifact`.

---

## ١ — القرار: ما يبقى وما يُحذف

### يُحذف

| الحقل | الحجم | لماذا يُحذف |
|---|---|---|
| `words` | ‎81.96٪‎ | ليست قياسًا بل قسمة. التهيئة المثبَّتة `alignment: "segment"`، و`models.aligner = null`، وكل كلمة تحمل `timing_source: "uniform_speech_spans"`. أُعيد بناؤها من `segments` + `speech_spans` فطابقت **‎15/15‎** بفارق < ‎10⁻⁹‎ ثانية |
| `cues` | ‎7.19٪‎ | مصدر ملفات **SRT/VTT** — وهي التي لا تريدها. مبنيّة أصلًا من `words` بدالّة `build_cues` |
| `transcript` | ‎2.37٪‎ | نسخة حرفية من `[s["text"] for s in segments]` — تحقّقت في **‎40/40‎** ملفًا |
| `implementation` | ‎1.77٪‎ | ‎60‎ بصمة `sha256` لملفات الحزمة، متطابقة في كل ملف. ‎63‎ ميغابايت من التكرار. تُكتب مرّة واحدة في `meta/` |
| `generated_tokens_by_segment` | ‎0.45٪‎ | عدّاد رموز لكل مقطع. حالة الإنذار الوحيدة التي تهمّ يغطّيها `token_limit_segments` صراحةً |
| `segmentation_details.speech_spans` | ‎2.6٪‎ | خريطة صمت `Silero VAD`. لا تذكرها الخطة في أيٍّ من مراحلها السبع — نفس محكّ `words` و`cues`. وهي المخرَج الوحيد هنا الذي يُعاد إنتاجه بلا معالج رسومي: Silero يعيدها من الـ`blob` على المعالج المركزي، ومعاملاتها محفوظة في `segmentation_details.parameters` |
| `source.path` | — | مسار محلّي مطلق داخل `.tmp/` لملف مؤقّت محذوف. لا يدلّ على شيء |
| المسافات البادئة | ‎26.6٪‎ من الباقي | `indent=2` في ملفات لا يفتحها إلا كود |

### يبقى

| الحقل | لماذا يبقى |
|---|---|
| `segments` | **الجوهر.** نصّ ومطلع ومقطع. هذا ما تشتريه ‎124‎ ساعة معالجة، وكل ما تطلبه M3 → M5 |
| `segmentation_details` (بلا `speech_spans`) | `merge` منه حقلٌ مثبَّت تقرأه `validate_artifact`، و`parameters` هي الوصفة التي تُعاد بها فترات الكلام إن طُلبت |
| `models` · `language` · `timing` · `segmentation` · `source.duration_seconds` | الحقول الستة المثبَّتة + المدّة. `validate_artifact` تقارنها بالتهيئة والاختلافُ إخفاقٌ لا هويّة جديدة |
| `schema_version` · `fallback_alignment_segments` · `repetition_detector_version` · `repetition_stopped_segments` · `truncation_retried_segments` · `token_limit_segments` | إشارات جودة بحجم يقارب الصفر، ويحتاجها طابور المراجعة في M3: تكرار أو بترٌ في مقطعٍ يعني تفريغًا مشكوكًا فيه |

> **ما يخسره حذف `speech_spans`:** الصمت المخبوء داخل المقاطع — ‎١٥٥‎ فجوة و‎١٥٨‎ ثانية في الملف الوسطي، أي ‎١٧٪‎ من زمن المقاطع. حدود المقاطع نفسها هي حدود فترات كلام (تحقّقت: ‎١٠٠٪‎)، فلا يُفقد حدٌّ واحد. استرجاعها يكلّف تنزيل `blobs/` وإعادة تشغيل VAD على المعالج المركزي — لا معالجًا رسوميًّا ولا إعادة تفريغ.

> **ملاحظة على SRT/VTT:** حذف `cues` لا يحرق المركب. لو احتجتَ ملفّ ترجمة يومًا، فـ`build_cues` + `generate_srt`/`generate_vtt` في الحزمة تُنتجه من `words`، و`words` تُنتَج من الباقي. أنت تحذف مخرجًا، لا القدرة عليه.

---

## ٢ — لماذا هذا آمن: أربعة ضمانات مفحوصة

1. **الـ`configHash` لا يتغيّر.** هو تجزئة الحقول الستة في `PINNED_CONFIG` — نموذج، مراجعة، لغة، `vad`، `vadMerge`، محاذاة. صيغة الملف ليست منها. **لا إعادة تفريغ.**
2. **`validate_artifact` تمرّ بلا أيّ تعديل عليها.** تقرأ `models.asr.id/.revision` و`language` و`segmentation` و`segmentation_details.merge` و`timing` و`source.duration_seconds` و`segments` — والسبعة كلّها في قائمة «يبقى». التنحيف شفّاف تجاهها.
3. **لا مستهلك آخر لهذه الآثار في المستودع.** مسحٌ لكل `get_object`/`json.loads` في `src/`: ثلاثة مواضع فقط تلمس `transcripts/`، وكلّها في `transcribe.py` وكلّها تمرّ عبر `validate_artifact`. لا شيء في `convex/` يقرأ الأثر.
4. **قانون ترتيب الكتابة §4.1 محفوظ.** التنحيف يقع قبل `put_object`، فالصفّ لا يصير `done` إلا وخلفه أثرٌ مكتمل. ونافذة الاسترداد §4.3 تقرأ الأثر النحيف وتصادق عليه وتُرقّي الصفّ بلا استدلال، كما كانت تمامًا.

---

## ٣ — الشكل قبل وبعد

```
قبل — 518.7 KB                          بعد — 29.7 KB
├─ schema_version                       ├─ schema_version
├─ implementation      ← 60 بصمة        │
│    artifacts_sha256/…                 │   (تُكتب مرّة في meta/)
├─ source                               ├─ source
│    path               ← مسار مؤقّت     │    duration_seconds
│    duration_seconds                   │    sample_rate
│    sample_rate                        │    decode_backend
│    decode_backend                     │    decode_fallback_reason
├─ language / segmentation / timing     ├─ language / segmentation / timing
├─ segmentation_details                 ├─ segmentation_details
│    parameters · merge                 │    parameters · merge
│    speech_spans      ← 401 فترة       │
├─ models {asr, vad, aligner:null}      ├─ models {asr, vad, aligner:null}
├─ repetition_* / truncation_* / …      ├─ repetition_* / truncation_* / …
├─ generated_tokens_by_segment          │
├─ transcript          ← تكرار          │
├─ segments            ← الجوهر         └─ segments            ← الجوهر
├─ words               ← 1,430 حسابية
└─ cues                ← 219 بطاقة SRT
```

---

## ٤ — التنفيذ

### خطوة ١ — دالّة التنحيف (`src/archive/transcribe.py`)

```python
# كل ما هنا إمّا حسابٌ على ما يبقى، أو تكرارٌ حرفيّ له.
DERIVED = ("implementation", "transcript", "words", "cues", "generated_tokens_by_segment")


def slim(data: dict) -> dict:
    """الأثر منقوصًا كلَّ ما يُعاد بناؤه منه. عديمة الأثر عند التكرار."""
    out = {k: v for k, v in data.items() if k not in DERIVED}
    # مسار ملفٍّ مؤقّت في .tmp/ حُذف قبل أن يُقرأ هذا السطر.
    out["source"] = {k: v for k, v in (out.get("source") or {}).items() if k != "path"}
    return out


def artifact_bytes(data: dict) -> bytes:
    """البايتات التي تُرفع إلى R2: نحيفة، بلا مسافات، عربية غير مهرَّبة."""
    return json.dumps(slim(data), ensure_ascii=False, separators=(",", ":")).encode()
```

`slim` عديمة الأثر عند التكرار (`slim(slim(x)) == slim(x)`) — وهذا ما يجعل خطوة ٣ قابلة للإعادة بلا حساب.

### خطوة ٢ — استعمالها عند الرفع (في `process_batch`)

```python
# قبل
body = artifact.read_bytes()
facts = validate_artifact(json.loads(body))

# بعد
data = json.loads(artifact.read_bytes())
facts = validate_artifact(data)   # يُصادَق على ما أنتجه النموذج كاملًا
body = artifact_bytes(data)       # ويُرفع منه ما لا يُعاد بناؤه
```

باقي الدالّة كما هو: `put_object` نفسه، والمفتاح نفسه، وترتيب الكتابة نفسه.

### خطوة ٣ — أمر ترحيل لما رُفع سابقًا: `archive slim-transcripts`

```python
def slim_published(config_hash: str = CONFIG_HASH, dry_run: bool = False, log=_log) -> dict:
    """يعيد كتابة كل أثر منشور في صورته النحيفة. آمن التكرار والانقطاع.

    تحت قفل المرحلة نفسه الذي يأخذه `transcribe`، لأنه يمسّ الآثار ذاتها:
    بدونه قد يُعاد كتابة أثرٍ ما يزال عاملٌ آخر يرفعه.
    """
    from . import pipeline

    with pipeline.stage(STAGE) as counts:
        s3, bucket = r2.client(), r2.bucket()
        before = after = 0
        rewritten = already = 0
        for key in sorted(r2.list_keys(s3, bucket, PREFIX)):
            if not key.endswith(f"/{config_hash}.json"):
                continue          # أثر تهيئةٍ أخرى: يُترك لـ reconcile، لا يُلمس
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            new = artifact_bytes(json.loads(body))
            before += len(body)
            after += len(new)
            if new == body:
                already += 1
                continue
            # لا يُكتب أبدًا ما كانت التهيئة سترفضه لاحقًا.
            validate_artifact(json.loads(new))
            if not dry_run:
                s3.put_object(
                    Bucket=bucket, Key=key, Body=new,
                    ContentType="application/json",
                    Metadata={"sha256": key_sha256(key), "configHash": config_hash},
                )
            rewritten += 1
            counts.processed += 1
        log(f"  {rewritten} أُعيدت كتابته، {already} نحيف أصلًا"
            f" — {before / 1e6:.0f} MB ← {after / 1e6:.0f} MB"
            + (" (تجربة جافّة — لم يُكتب شيء)" if dry_run else ""))
        return {"rewritten": rewritten, "already": already,
                "bytesBefore": before, "bytesAfter": after, "dryRun": dry_run}
```

وفي `cli.py`، على منوال `reconcile-artifacts`:

```python
thin = sub.add_parser("slim-transcripts", help="أعد كتابة الآثار المنشورة نحيفة")
thin.add_argument("--dry-run", action="store_true", help="اعرض الفرق، لا تكتب")
thin.set_defaults(func=cmd_slim)
```

**ثلاث خصائص مقصودة:** `put_object` على المفتاح نفسه استبدالٌ ذرّي — القارئ يرى القديم أو الجديد، لا ملفًّا ممزَّقًا؛ ولا حذف قطّ، فقانون §4.4-ج «لا يُحذف أثرٌ أرشيفيّ تلقائيًّا» يبقى قائمًا؛ والانقطاع في المنتصف يعني إعادة التشغيل تتخطّى ما نُحّف (`new == body`) وتُكمل.

### خطوة ٤ — كتلة `implementation` مرّة واحدة

أثناء المرور في خطوة ٣، تُجمع الكتل المتمايزة وتُكتب إلى:

```
meta/implementation/{sha256(canonical_json(block))[:12]}.json
```

المتوقّع ملفّ واحد لكل ‎9,793‎ أثرًا. إن ظهر أكثر من واحد، فذلك خبرٌ يستحق أن يُطبع: الحزمة تغيّرت في منتصف الحملة.

---

## ٥ — الاختبار

ملف واحد على منوال `tests/test_m0.py` — بلا إطار، `python tests/test_slim.py`:

```python
full = json.loads(SAMPLE.read_bytes())
thin = transcribe.slim(full)

assert transcribe.slim(thin) == thin                                    # آمن التكرار
assert transcribe.validate_artifact(thin) == transcribe.validate_artifact(full)
assert thin["segments"] == full["segments"]                             # الجوهر سليم
assert "speech_spans" not in thin["segmentation_details"]               # وخريطة الصمت كذلك
assert not set(thin) & set(transcribe.DERIVED)                          # لم يبقَ مشتقّ
assert b"\n  " not in transcribe.artifact_bytes(full)                   # بلا مسافات بادئة
```

السطر الثاني هو الاختبار الحقيقي: إن أسقط التنحيف يومًا حقلًا مثبَّتًا، يسقط هنا قبل أن يسقط في R2.

---

## ٦ — التوقيت

الحملة تعمل الآن: ‎2,003‎ منجَزًا، ‎500‎ قيد المعالجة، ‎7,290‎ باقية. والقفل يمنع تشغيل `slim-transcripts` بالتوازي معها — وهذا مقصود.

| # | الخطوة | الأثر |
|---|---|---|
| ١ | نفِّذ خطوات ١–٤ وادفعها | لا شيء بعد؛ الحملة الجارية لا تلتقط تغيير الكود |
| ٢ | `Ctrl-C` على الحملة | يوقفها فعلًا (commit `22ab147`). صفوف `processing` تُستردّ بعد نافذة الخمس دقائق |
| ٣ | `archive slim-transcripts --dry-run` ثم بلا `--dry-run` | كل أثر منشور: نحو ‎95.7٪‎ توفيرًا. دقائق معدودة |
| ٤ | `archive transcribe` من جديد | الصفوف المنجَزة تُتخطّى بمسار §4.2 السريع. الـ‎7,290‎ الباقية تُولد نحيفة |
| ٥ | عند انتهاء الحملة: `archive slim-transcripts` مرّة أخيرة | يجب أن يقول «‎0‎ أُعيدت كتابته». هذا هو التحقّق، لا عملٌ إضافي |

**بديل إن لم ترغب في لمس الحملة الجارية أصلًا:** نفِّذ الخطوات ١–٤ برمجيًّا، ودع الحملة تكمل الـ‎100‎ ساعة الباقية كما هي، ثم شغّل `slim-transcripts` مرّة واحدة في النهاية. الثمن: كتابة ‎5‎ غيغابايت ثم إعادة كتابتها — وهي في R2 نحو ‎0.05$‎ من عمليات الكتابة. أرخص من أيّ قلق.

---

## ٧ — الأرقام المتوقّعة

| الصيغة | لكل ملف | الأرشيف (‎9,793‎) | النسبة |
|---|---|---|---|
| اليوم | 518.7 KB | 5,201 MB | 100٪ |
| مع إبقاء `speech_spans` | 29.7 KB | 297 MB | 5.7٪ |
| **النحيف (المنفَّذ)** | **22.2 KB** | **223 MB** | **4.3٪** |
| النحيف + `gzip` | 9.1 KB | 92 MB | 1.8٪ |

مقيسة على عيّنة ‎50‎ ملفًا بالشكل المقترح حرفيًّا، لا تقديرًا.

**`gzip` مرفوض قصدًا:** آليّةٌ ثانية على مجموعةٍ صارت ‎223‎ ميغابايت أصلًا؛ تجعل كل أثرٍ غير قابل للقراءة إلا بفكّ ضغط، مقابل توفيرٍ لم يعد يعني شيئًا. تغييرٌ واحد، لا اثنان.

---

## ٨ — ما لا يُفعل

- **لا مساس بـ`PINNED_CONFIG`.** الحقول الستة تُهشَّم إلى الـ`configHash`، وأيّ تغيير فيها يعيد تفريغ الأرشيف من الصفر. صيغة الملف ليست منها، وهذا بالضبط ما يجعل هذه الخطة مجّانية.
- **لا حذف ثم كتابة.** استبدالٌ ذرّي بـ`put_object` على المفتاح نفسه، وحسب.
- **لا تشغيل لـ`slim-transcripts` بالتوازي مع `transcribe`.** قفل المرحلة يمنعه، ولا يُلتفّ عليه.
- **لا لمس لآثار `configHash` آخر.** الشرط `key.endswith(f"/{config_hash}.json")` يحرسها؛ شأنها إلى `reconcile-artifacts`.
- **لا تعديل على الحزمة المضمَّنة `cohere-transcribe` أثناء الحملة** — يغيّر بصمات `implementation` فينقسم الأرشيف إلى قبل وبعد بلا سبب.
- **لا حذف لـ`segments` ولا للحقول الستة المثبَّتة**، مهما بلغ إغراء الرقم. هذا هو الحدّ الذي لا يُتجاوز.
