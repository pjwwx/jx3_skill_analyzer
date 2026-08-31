// JX3 PakV4 extraction helper.
//
// The GUI stages this statically linked helper temporarily in the selected
// bin64 directory.  This preserves the game's application-directory DLL search
// behavior without asking users to copy or launch anything manually.  The
// caller supplies a GBK encoded path list and an arbitrary output directory.

#include <algorithm>
#include <clocale>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include <windows.h>
#include <psapi.h>

namespace fs = std::filesystem;

class IFile;
using G_OpenFile = IFile* (const char*, unsigned int, unsigned int);
using G_IsFileExist = bool (const char*);
using G_FileNameHash = int (const char*);
using INITV4 = int (const char*, const char*, void*, int, int, int);
using G_setlocale = char* __cdecl(int, const char*);

class IFile {
public:
    virtual unsigned long Read(void* buffer, unsigned long readBytes) = 0;
    virtual unsigned long Write(const void* buffer, unsigned long writeBytes) = 0;
    virtual void* GetBuffer() = 0;
    virtual long Seek(long offset, int origin) = 0;
    virtual long Tell() = 0;
    virtual unsigned long Size() = 0;
    virtual int IsFileInPak() = 0;
    virtual int IsPackedByFragment() = 0;
    virtual int GetFragmentCount() = 0;
    virtual unsigned int GetFragmentSize(int fragmentIndex) = 0;
    virtual unsigned long ReadFragment(int fragmentIndex, void*& buffer) = 0;
    virtual void Close() = 0;
    virtual void Release() = 0;
    virtual ~IFile() = default;
};

struct Options {
    fs::path bin64;
    fs::path listFile;
    fs::path output;
    bool overwrite = false;
    bool verbose = false;
};

static void print_usage() {
    std::cout
        << "JX3PakBridge --bin64 <game-bin64> --list <gbk-path-list> "
           "--output <directory> [--overwrite]\n";
}

static bool parse_options(int argc, wchar_t** argv, Options& options) {
    for (int index = 1; index < argc; ++index) {
        const std::wstring arg(argv[index]);
        if (arg == L"--overwrite") {
            options.overwrite = true;
            continue;
        }
        if (arg == L"--verbose") {
            options.verbose = true;
            continue;
        }
        if ((arg == L"--bin64" || arg == L"--list" || arg == L"--output") &&
            index + 1 < argc) {
            const fs::path value(argv[++index]);
            if (arg == L"--bin64") {
                options.bin64 = value;
            } else if (arg == L"--list") {
                options.listFile = value;
            } else {
                options.output = value;
            }
            continue;
        }
        return false;
    }
    return !options.bin64.empty() && !options.listFile.empty() && !options.output.empty();
}

static std::string wide_to_codepage(const std::wstring& value, UINT codepage) {
    if (value.empty()) {
        return {};
    }
    const int size = WideCharToMultiByte(
        codepage, 0, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    if (size <= 0) {
        return {};
    }
    std::string result(static_cast<size_t>(size), '\0');
    WideCharToMultiByte(
        codepage, 0, value.data(), static_cast<int>(value.size()), result.data(), size, nullptr, nullptr);
    return result;
}

static std::wstring codepage_to_wide(const std::string& value, UINT codepage) {
    if (value.empty()) {
        return {};
    }
    const int size = MultiByteToWideChar(
        codepage, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0);
    if (size <= 0) {
        return {};
    }
    std::wstring result(static_cast<size_t>(size), L'\0');
    MultiByteToWideChar(
        codepage, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), result.data(), size);
    return result;
}

static std::string trim_ascii(std::string value) {
    if (value.size() >= 3 &&
        static_cast<unsigned char>(value[0]) == 0xEF &&
        static_cast<unsigned char>(value[1]) == 0xBB &&
        static_cast<unsigned char>(value[2]) == 0xBF) {
        value.erase(0, 3);
    }
    while (!value.empty() &&
           (value.back() == '\r' || value.back() == '\n' || value.back() == ' ' || value.back() == '\t')) {
        value.pop_back();
    }
    size_t start = 0;
    while (start < value.size() && (value[start] == ' ' || value[start] == '\t')) {
        ++start;
    }
    if (start != 0) {
        value.erase(0, start);
    }
    if (value.size() >= 2 && value.front() == '"' && value.back() == '"') {
        value = value.substr(1, value.size() - 2);
    }
    return value;
}

static bool safe_relative_path(const fs::path& value) {
    if (value.empty() || value.is_absolute() || value.has_root_name() || value.has_root_directory()) {
        return false;
    }
    for (const auto& part : value) {
        if (part == L".." || part == L".") {
            return false;
        }
    }
    return true;
}

static void set_loaded_modules_locale_936() {
    HMODULE modules[4096];
    DWORD needed = 0;
    if (!EnumProcessModules(GetCurrentProcess(), modules, sizeof(modules), &needed)) {
        return;
    }
    const DWORD count = std::min<DWORD>(needed / sizeof(HMODULE), 4096);
    for (DWORD index = 0; index < count; ++index) {
        auto module_setlocale = reinterpret_cast<G_setlocale*>(
            GetProcAddress(modules[index], "setlocale"));
        if (module_setlocale != nullptr) {
            module_setlocale(LC_ALL, "chinese_china.936");
        }
    }
}

int wmain(int argc, wchar_t** argv) {
    Options options;
    if (!parse_options(argc, argv, options)) {
        print_usage();
        return 64;
    }

    std::error_code error;
    options.bin64 = fs::absolute(options.bin64, error).lexically_normal();
    options.listFile = fs::absolute(options.listFile, error).lexically_normal();
    options.output = fs::absolute(options.output, error).lexically_normal();

    const fs::path enginePath = options.bin64 / L"Engine_Lua5X64.dll";
    const fs::path gameRoot = options.bin64.parent_path().parent_path().parent_path();
    const fs::path pakRoot = gameRoot / L"PakV4";
    const fs::path trunkPath = pakRoot / L"Trunk.dir";

    if (!fs::is_regular_file(enginePath)) {
        std::cerr << "ERROR engine_not_found\n";
        return 65;
    }
    if (!fs::is_regular_file(trunkPath)) {
        std::cerr << "ERROR pakv4_not_found\n";
        return 66;
    }
    if (!fs::is_regular_file(options.listFile)) {
        std::cerr << "ERROR list_not_found\n";
        return 67;
    }

    fs::create_directories(options.output, error);
    if (error) {
        std::cerr << "ERROR output_unavailable\n";
        return 68;
    }

    // Match the game's loader sequence: Engine_Lua5X64.dll is loaded while the
    // working directory is bin64.  The original extractor got this behavior by
    // requiring its own EXE to live there; we derive it from the selected path.
    fs::current_path(options.bin64, error);
    if (error) {
        std::cerr << "ERROR bin64_directory_unavailable\n";
        return 74;
    }

    if (options.verbose) {
        std::cerr << "TRACE loading_engine\n";
    }
    HMODULE engine = LoadLibraryA("Engine_Lua5X64.dll");
    if (engine == nullptr) {
        std::cerr << "ERROR engine_load_failed code=" << GetLastError() << "\n";
        return 69;
    }

    auto openFile = reinterpret_cast<G_OpenFile*>(GetProcAddress(engine, "g_OpenFile"));
    auto fileExists = reinterpret_cast<G_IsFileExist*>(GetProcAddress(engine, "g_IsFileExist"));
    auto fileNameHash = reinterpret_cast<G_FileNameHash*>(GetProcAddress(engine, "g_FileNameHash"));
    auto initPakV4 = reinterpret_cast<INITV4*>(GetProcAddress(engine, "KG_InitPakV4FileSystem"));
    if (openFile == nullptr || fileExists == nullptr || fileNameHash == nullptr || initPakV4 == nullptr) {
        std::cerr << "ERROR engine_exports_missing\n";
        return 70;
    }
    if (options.verbose) {
        std::cerr << "TRACE engine_ready\n";
    }

    // Package paths are resolved from the language-pack directory after the
    // engine module has loaded, again mirroring the working original.
    fs::current_path(options.bin64.parent_path(), error);
    if (error) {
        std::cerr << "ERROR locale_directory_unavailable\n";
        return 75;
    }

    set_loaded_modules_locale_936();
    // This engine export expects a path relative to the language-pack working
    // directory.  Compute it from the selected installation; do not tie it to
    // the helper executable's own directory.
    const fs::path pakRootForEngine = pakRoot.lexically_relative(options.bin64.parent_path());
    const std::string pakRootGbk = wide_to_codepage(
        (pakRootForEngine.empty() ? pakRoot.generic_wstring() : pakRootForEngine.generic_wstring()), 936);
    if (options.verbose) {
        std::cerr << "TRACE initializing_pakv4 root=" << pakRootGbk << "\n";
    }
    if (pakRootGbk.empty() || !initPakV4(pakRootGbk.c_str(), "Trunk.Dir", nullptr, 0, 0, 0)) {
        std::cerr << "ERROR pakv4_init_failed\n";
        return 71;
    }
    if (options.verbose) {
        std::cerr << "TRACE pakv4_ready\n";
    }

    std::ifstream list(options.listFile, std::ios::binary);
    if (!list.is_open()) {
        std::cerr << "ERROR list_open_failed\n";
        return 72;
    }

    unsigned long long total = 0;
    unsigned long long extracted = 0;
    unsigned long long skipped = 0;
    unsigned long long missing = 0;
    unsigned long long invalid = 0;
    unsigned long long failed = 0;

    std::string internalPath;
    while (std::getline(list, internalPath)) {
        internalPath = trim_ascii(std::move(internalPath));
        if (internalPath.empty()) {
            continue;
        }
        ++total;
        std::replace(internalPath.begin(), internalPath.end(), '/', '\\');
        if (options.verbose) {
            std::cerr << "TRACE checking_file index=" << total << "\n";
        }

        const std::wstring relativeWide = codepage_to_wide(internalPath, 936);
        const fs::path relativePath(relativeWide);
        if (relativeWide.empty() || !safe_relative_path(relativePath)) {
            ++invalid;
            continue;
        }

        // Calling the hash function preserves the initialization behavior used
        // by the original tool, even though the value is not needed for output.
        fileNameHash(internalPath.c_str());
        if (options.verbose) {
            std::cerr << "TRACE hash_ready index=" << total << "\n";
        }
        if (!fileExists(internalPath.c_str())) {
            ++missing;
            if (options.verbose) {
                std::cerr << "TRACE file_missing index=" << total << "\n";
            }
            continue;
        }
        if (options.verbose) {
            std::cerr << "TRACE opening_file index=" << total << "\n";
        }

        const fs::path outputPath = (options.output / relativePath).lexically_normal();
        if (!options.overwrite && fs::exists(outputPath)) {
            ++skipped;
            continue;
        }

        IFile* packageFile = openFile(internalPath.c_str(), 0, 0);
        if (packageFile == nullptr) {
            ++failed;
            continue;
        }

        const unsigned long expectedSize = packageFile->Size();
        if (options.verbose) {
            std::cerr << "TRACE file_opened index=" << total << " size=" << expectedSize << "\n";
        }
        std::vector<char> bytes(static_cast<size_t>(expectedSize));
        const unsigned long actualSize = expectedSize == 0
            ? 0
            : packageFile->Read(bytes.data(), expectedSize);
        if (options.verbose) {
            std::cerr << "TRACE file_read index=" << total << " size=" << actualSize << "\n";
        }
        packageFile->Release();
        if (options.verbose) {
            std::cerr << "TRACE file_released index=" << total << "\n";
        }

        fs::create_directories(outputPath.parent_path(), error);
        if (error) {
            error.clear();
            ++failed;
            continue;
        }
        std::ofstream output(outputPath, std::ios::binary | std::ios::trunc);
        if (!output.is_open()) {
            ++failed;
            continue;
        }
        if (actualSize != 0) {
            output.write(bytes.data(), static_cast<std::streamsize>(actualSize));
        }
        output.close();
        if (output.fail()) {
            ++failed;
            continue;
        }
        ++extracted;
        if (options.verbose) {
            std::cerr << "TRACE file_written index=" << total << "\n";
        }
    }

    list.close();
    if (options.verbose) {
        std::cerr << "TRACE extraction_complete\n";
    }

    // Do not unload the game engine explicitly.  Some client builds retain
    // PakV4 callbacks in lazily loaded modules and can fault during manual DLL
    // teardown.  This helper exits immediately, so Windows safely releases all
    // process modules and package resources in dependency order.

    std::cout
        << "SUMMARY total=" << total
        << " extracted=" << extracted
        << " skipped=" << skipped
        << " missing=" << missing
        << " invalid=" << invalid
        << " failed=" << failed
        << std::endl;
    return failed == 0 ? 0 : 73;
}
