"""让翻译用的 ONNX Runtime 与 sherpa 自带的 ORT 在同一进程里共存。

问题：一个进程里只能有一份 ``libonnxruntime.so``，而 sherpa-onnx（ASR）和
onnxruntime-android（翻译）各带一份，版本还不同（sherpa 是 1.28.2 定制构建，
官方 AAR 是 1.28.x 发布版）。硬凑的结果是：

    cannot locate symbol "OrtGetApiBase" referenced by libonnxruntime4j_jni.so

两边的符号表里都有 ``OrtGetApiBase``——因为 GNU 符号版本不匹配，链接器直接当它
不存在。（试过把版本串与版本索引都改签成提供方的值，bionic 仍然拒绝；这条路不通。）

现在的做法：**不动符号版本，改 soname**。把官方那一对（``libonnxruntime.so`` +
``libonnxruntime4j_jni.so``）当成私有运行时，soname 与 DT_NEEDED 一起改成
``libonnxrt_1280.so``。于是进程里有两份 ORT：sherpa 的 ``libonnxruntime.so``
给它自己用，``libonnxrt_1280.so`` 只给翻译用，各自与自己的 JNI 精确配对，
谁都不用将就谁。代价是安装包多约 16 MB。

关键约束：``libonnxruntime.so`` 与 ``libonnxrt_1280.so`` **都是 17 字节**，
所以全部改写都是等长替换，不移动任何偏移。

用法::

    python tools/pin_ort_jni.py build/ort1280 --name libonnxrt_1280.so
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ABIS = ("arm64-v8a", "x86_64")
DEFAULT_SONAME = "libonnxruntime.so"
ROOT = Path(__file__).resolve().parent.parent

# Maven Central 直连在本机常年超时，镜像按顺序试。
MIRRORS = (
    "https://maven.aliyun.com/repository/central",
    "https://maven.aliyun.com/repository/public",
    "https://repo.huaweicloud.com/repository/maven",
    "https://repo1.maven.org/maven2",
)


def ensure_aar(version: str, target: Path) -> Path:
    """下载并解包 onnxruntime-android 的 AAR，返回解包目录（已有则直接复用）。"""
    if (target / "jni" / "x86_64" / "libonnxruntime4j_jni.so").is_file():
        return target
    coord = f"com/microsoft/onnxruntime/onnxruntime-android/{version}/onnxruntime-android-{version}.aar"
    target.parent.mkdir(parents=True, exist_ok=True)
    aar = target.parent / f"onnxruntime-android-{version}.aar"
    if not aar.is_file():
        last = None
        for base in MIRRORS:
            try:
                print(f"下载 {base}/…/{aar.name}")
                with urllib.request.urlopen(f"{base}/{coord}", timeout=120) as response:
                    aar.write_bytes(response.read())
                break
            except Exception as error:  # noqa: BLE001 - 镜像挨个试，最后统一报错
                last = error
                print(f"  失败：{error}")
        else:
            raise SystemExit(f"所有镜像都下不到 {version}：{last}")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(aar) as archive:
        archive.extractall(target)
    return target


def rename_needed(path: Path, old: str, new: str) -> int:
    """把 soname / DT_NEEDED / verneed 文件名字符串原地改掉，返回替换次数。"""
    old_b, new_b = old.encode(), new.encode()
    if len(old_b) != len(new_b):
        raise SystemExit(f"两个名字必须等长，否则会移动 .dynstr 偏移：{old} vs {new}")
    data = path.read_bytes()
    count = data.count(old_b)
    if count == 0:
        raise SystemExit(f"{path.name} 里找不到 {old}")
    path.write_bytes(data.replace(old_b, new_b))
    return count


def check_soname(path: Path, expected: str) -> str:
    try:
        from elftools.elf.elffile import ELFFile
    except ImportError:  # pragma: no cover
        return "skipped"
    with path.open("rb") as handle:
        dynamic = ELFFile(handle).get_section_by_name(".dynamic")
        if dynamic is None:
            return "no-dynamic"
        for tag in dynamic.iter_tags():
            if tag.entry.d_tag == "DT_SONAME":
                if tag.soname != expected:
                    raise SystemExit(f"{path.name} 的 SONAME 是 {tag.soname}，期望 {expected}")
                return tag.soname
    return "no-soname"


def main() -> int:
    parser = argparse.ArgumentParser(description="给官方 ORT 换 soname，避开与 sherpa 的冲突")
    parser.add_argument(
        "aar_dir",
        type=Path,
        nargs="?",
        default=None,
        help="解包后的 AAR 目录（含 jni/ 与 classes.jar）；省略则自动下载到 build/ort-<版本>",
    )
    parser.add_argument("--name", default="libonnxrt_1280.so", help="私有运行时文件名（必须与原名等长）")
    parser.add_argument("--out", type=Path, default=Path("mobile/android/app/src/main/jniLibs"))
    parser.add_argument("--jar-out", type=Path, default=Path("mobile/android/app/libs"))
    parser.add_argument("--version", default="1.28.0", help="onnxruntime-android 版本，也用于 jar 命名")
    args = parser.parse_args()

    aar_dir = args.aar_dir or ensure_aar(args.version, ROOT / "build" / f"ort-{args.version}")
    if not (aar_dir / "jni").is_dir():
        aar_dir = ensure_aar(args.version, ROOT / "build" / f"ort-{args.version}")

    for abi in ABIS:
        src_dir = aar_dir / "jni" / abi
        jni = src_dir / "libonnxruntime4j_jni.so"
        ort = src_dir / DEFAULT_SONAME
        if not jni.is_file() or not ort.is_file():
            print(f"跳过 {abi}：AAR 里缺 JNI 或运行时", file=sys.stderr)
            continue
        out_dir = args.out / abi
        out_dir.mkdir(parents=True, exist_ok=True)

        # 运行时：改 SONAME，文件名同步改
        target_ort = out_dir / args.name
        shutil.copy2(ort, target_ort)
        renamed = rename_needed(target_ort, DEFAULT_SONAME, args.name)
        soname = check_soname(target_ort, args.name)

        # JNI：把 DT_NEEDED 与 verneed 里的文件名一起改指过来
        target_jni = out_dir / jni.name
        shutil.copy2(jni, target_jni)
        needed = rename_needed(target_jni, DEFAULT_SONAME, args.name)

        print(f"{abi}: {target_ort.name} SONAME={soname}（替换 {renamed} 处）")
        print(f"{abi}: {target_jni.name} 依赖改指 {args.name}（替换 {needed} 处）")

    jar = aar_dir / "classes.jar"
    if jar.is_file():
        args.jar_out.mkdir(parents=True, exist_ok=True)
        for stale in args.jar_out.glob("onnxruntime-android-*.jar"):
            stale.unlink()
            print(f"移除旧 jar {stale.name}")
        target = args.jar_out / f"onnxruntime-android-{args.version}.jar"
        shutil.copy2(jar, target)
        print(f"jar: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
