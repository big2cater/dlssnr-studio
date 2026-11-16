// Self-extracting launcher for the DLSSNR-AMD desktop app.
//
// The shipped file is
//
//     [ this launcher ][ payload.zip ][ 32-byte footer ]
//
// The footer records where the zip starts and which build it is. On the first
// run the payload is unpacked once into %LOCALAPPDATA%\NRStudio\<id> and the
// real program is started from there; every later run skips straight to it.
// Unpacking once (instead of on every start, the way a plain one-file archive
// does) keeps the second launch instant and leaves the models on disk where the
// renderer can map them.
//
// tar.exe ships with Windows 10 1803+ and reads zip through libarchive, so the
// launcher needs no decompression code of its own.
//
//   clang-cl /std:c++17 /O2 /EHsc /DUNICODE /D_UNICODE launcher.cpp ^
//       /Fe:launcher.exe /link /SUBSYSTEM:WINDOWS user32.lib shell32.lib comctl32.lib

#include <windows.h>
#include <commctrl.h>
#include <shellapi.h>
#include <shlobj.h>

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <cwctype>
#include <iterator>
#include <string>

namespace {

constexpr char kMagic[8] = {'N', 'R', 'P', 'A', 'C', 'K', '0', '1'};
constexpr DWORD kFooterSize = 32;

struct Footer {
    char magic[8];
    std::uint64_t payload_size;
    char id[8];
    std::uint64_t reserved;
};
static_assert(sizeof(Footer) == kFooterSize, "footer layout must stay fixed");

constexpr UINT WM_APP_UNPACKED = WM_APP + 1;
constexpr int kIdBar = 1002;
constexpr int kWindowWidth = 520;
constexpr int kWindowHeight = 176;

HINSTANCE g_instance = nullptr;
HWND g_window = nullptr;
std::wstring g_target;  // %LOCALAPPDATA%\NRStudio\<id>
std::wstring g_error;

// ------------------------------------------------------------------ helpers

// First-run diagnostics. Only the unpacking path writes here: a warm start is
// already fast and silent, and a failure with no trace at all is impossible to
// debug on a machine you cannot attach a debugger to.
void trace(const std::wstring& line) {
    wchar_t temp[MAX_PATH * 2];
    GetTempPathW(static_cast<DWORD>(std::size(temp)), temp);
    const std::wstring path = std::wstring(temp) + L"nrstudio.launch.log";
    HANDLE file = CreateFileW(path.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ, nullptr,
                              OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return;
    const std::wstring text = line + L"\r\n";
    DWORD written = 0;
    WriteFile(file, text.c_str(), static_cast<DWORD>(text.size() * sizeof(wchar_t)), &written,
              nullptr);
    CloseHandle(file);
}

std::wstring exe_path() {
    wchar_t buffer[MAX_PATH * 4];
    const DWORD n = GetModuleFileNameW(nullptr, buffer, static_cast<DWORD>(std::size(buffer)));
    return std::wstring(buffer, n);
}

std::wstring local_app_data() {
    PWSTR raw = nullptr;
    std::wstring out;
    if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_LocalAppData, 0, nullptr, &raw)) && raw) {
        out = raw;
    }
    CoTaskMemFree(raw);
    return out;
}

std::wstring dir_name(const std::wstring& path) {
    const auto slash = path.find_last_of(L"\\/");
    return slash == std::wstring::npos ? path : path.substr(0, slash);
}

bool file_exists(const std::wstring& path) {
    const DWORD attr = GetFileAttributesW(path.c_str());
    return attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY);
}

// Everything after argv[0], quoted so paths with spaces survive the hand-off.
std::wstring forwarded_args() {
    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) return L"";
    std::wstring out;
    for (int i = 1; i < argc; ++i) {
        out += L" \"";
        out += argv[i];
        out += L'"';
    }
    LocalFree(argv);
    return out;
}

bool read_footer(const std::wstring& self, Footer& footer, std::uint64_t& file_size) {
    HANDLE file = CreateFileW(self.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                              FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;

    LARGE_INTEGER size{};
    bool ok = false;
    if (GetFileSizeEx(file, &size) && size.QuadPart > kFooterSize) {
        LARGE_INTEGER at{};
        at.QuadPart = size.QuadPart - kFooterSize;
        DWORD got = 0;
        if (SetFilePointerEx(file, at, nullptr, FILE_BEGIN) &&
            ReadFile(file, &footer, kFooterSize, &got, nullptr) && got == kFooterSize) {
            file_size = static_cast<std::uint64_t>(size.QuadPart);
            ok = !std::memcmp(footer.magic, kMagic, sizeof(kMagic));
        }
    }
    CloseHandle(file);
    return ok;
}

bool copy_payload(const std::wstring& self, const Footer& footer, std::uint64_t file_size,
                  const std::wstring& zip_path) {
    const std::uint64_t offset = file_size - kFooterSize - footer.payload_size;
    HANDLE src = CreateFileW(self.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                             FILE_ATTRIBUTE_NORMAL, nullptr);
    if (src == INVALID_HANDLE_VALUE) return false;
    HANDLE dst = CreateFileW(zip_path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS,
                             FILE_ATTRIBUTE_NORMAL, nullptr);
    if (dst == INVALID_HANDLE_VALUE) {
        CloseHandle(src);
        return false;
    }

    LARGE_INTEGER at{};
    at.QuadPart = static_cast<LONGLONG>(offset);
    bool ok = SetFilePointerEx(src, at, nullptr, FILE_BEGIN) != FALSE;

    // 8 MB chunks: the payload is a few hundred MB, so a larger buffer only
    // wastes working set and a smaller one turns into syscall overhead.
    constexpr DWORD kChunk = 8u << 20;
    auto* buffer = static_cast<char*>(std::malloc(kChunk));
    std::uint64_t left = footer.payload_size;
    while (ok && left > 0 && buffer) {
        const DWORD want = static_cast<DWORD>(left < kChunk ? left : kChunk);
        DWORD got = 0;
        if (!ReadFile(src, buffer, want, &got, nullptr) || got == 0) {
            ok = false;
            break;
        }
        DWORD written = 0;
        if (!WriteFile(dst, buffer, got, &written, nullptr) || written != got) {
            ok = false;
            break;
        }
        left -= got;
    }
    std::free(buffer);
    CloseHandle(dst);
    CloseHandle(src);
    if (!ok) DeleteFileW(zip_path.c_str());
    return ok;
}

// Runs a command line and waits. tar.exe is quiet on success, so the exit code
// is what tells us whether the payload actually landed.
bool run_and_wait(const std::wstring& command_line) {
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;  // no console flash behind the progress window
    PROCESS_INFORMATION pi{};
    std::wstring mutable_cmd = command_line;
    if (!CreateProcessW(nullptr, mutable_cmd.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW,
                        nullptr, nullptr, &si, &pi)) {
        return false;
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return code == 0;
}

void remove_tree(const std::wstring& path) {
    WIN32_FIND_DATAW found{};
    HANDLE hunt = FindFirstFileW((path + L"\\*").c_str(), &found);
    if (hunt != INVALID_HANDLE_VALUE) {
        do {
            const std::wstring name = found.cFileName;
            if (name == L"." || name == L"..") continue;
            const std::wstring child = path + L"\\" + name;
            if (found.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
                remove_tree(child);
            } else {
                SetFileAttributesW(child.c_str(), FILE_ATTRIBUTE_NORMAL);
                DeleteFileW(child.c_str());
            }
        } while (FindNextFileW(hunt, &found));
        FindClose(hunt);
    }
    RemoveDirectoryW(path.c_str());
}

// Only directories that look like ours are touched: 8 hex characters, and not
// the build we just unpacked. Failures (a build still running) are ignored.
void drop_old_builds(const std::wstring& root, const std::wstring& keep) {
    WIN32_FIND_DATAW found{};
    HANDLE hunt = FindFirstFileW((root + L"\\*").c_str(), &found);
    if (hunt == INVALID_HANDLE_VALUE) return;
    do {
        if (!(found.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
        const std::wstring name = found.cFileName;
        if (name.size() != 8 || name == keep) continue;
        bool hex = true;
        for (wchar_t ch : name) {
            if (!std::iswxdigit(ch)) {
                hex = false;
                break;
            }
        }
        if (hex) remove_tree(root + L"\\" + name);
    } while (FindNextFileW(hunt, &found));
    FindClose(hunt);
}

bool launch_app() {
    const std::wstring exe = g_target + L"\\nr-app.exe";
    std::wstring command = L"\"" + exe + L"\"" + forwarded_args();
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};
    if (!CreateProcessW(exe.c_str(), command.data(), nullptr, nullptr, FALSE, 0, nullptr,
                        g_target.c_str(), &si, &pi)) {
        return false;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return true;
}

// ------------------------------------------------------------------- worker

DWORD WINAPI unpack_thread(void*) {
    trace(L"unpack: start");
    wchar_t temp[MAX_PATH * 2];
    GetTempPathW(static_cast<DWORD>(std::size(temp)), temp);
    const std::wstring zip = std::wstring(temp) + L"nrstudio_" +
                             std::to_wstring(GetCurrentProcessId()) + L".zip";

    const std::wstring self = exe_path();
    Footer footer{};
    std::uint64_t file_size = 0;
    const bool header = read_footer(self, footer, file_size);
    trace(L"unpack: footer " + std::to_wstring(header ? 1 : 0) + L" size " +
          std::to_wstring(file_size));
    bool ok = header && copy_payload(self, footer, file_size, zip);
    trace(L"unpack: copy " + std::to_wstring(ok ? 1 : 0));
    if (ok) {
        // CreateDirectoryW only makes the last component; the parent
        // %LOCALAPPDATA%\NRStudio does not exist on a first run.
        const int made = SHCreateDirectoryExW(nullptr, g_target.c_str(), nullptr);
        trace(L"unpack: mkdir " + std::to_wstring(made));
        wchar_t system[MAX_PATH];
        GetSystemDirectoryW(system, MAX_PATH);
        const std::wstring command = L"\"" + std::wstring(system) + L"\\tar.exe\" -xf \"" + zip +
                                     L"\" -C \"" + g_target + L"\"";
        trace(L"unpack: tar " + command);
        ok = run_and_wait(command);
        trace(L"unpack: tar rc " + std::to_wstring(ok ? 0 : 1));
        if (!ok) g_error = L"解压失败（tar 返回错误）。请确认磁盘空间充足后重试。";
    } else if (g_error.empty()) {
        g_error = L"无法读取内嵌数据：这个文件可能没有下载完整。";
    }
    DeleteFileW(zip.c_str());

    if (ok) {
        const std::wstring ready = g_target + L"\\.nr_ready";
        HANDLE mark = CreateFileW(ready.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS,
                                  FILE_ATTRIBUTE_HIDDEN, nullptr);
        if (mark != INVALID_HANDLE_VALUE) {
            DWORD written = 0;
            WriteFile(mark, footer.id, sizeof(footer.id), &written, nullptr);
            CloseHandle(mark);
        }
        drop_old_builds(dir_name(g_target), g_target.substr(g_target.find_last_of(L'\\') + 1));
    }
    PostMessageW(g_window, WM_APP_UNPACKED, ok ? 1 : 0, 0);
    return 0;
}

// -------------------------------------------------------------------- window

LRESULT CALLBACK window_proc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    switch (message) {
    case WM_CREATE: {
        LOGFONTW lf{};
        GetObjectW(GetStockObject(DEFAULT_GUI_FONT), sizeof(lf), &lf);
        lf.lfHeight = -22;
        lf.lfWeight = FW_SEMIBOLD;
        lstrcpynW(lf.lfFaceName, L"Microsoft YaHei UI", LF_FACESIZE);
        HFONT title_font = CreateFontIndirectW(&lf);

        lf.lfHeight = -14;
        lf.lfWeight = FW_NORMAL;
        HFONT body_font = CreateFontIndirectW(&lf);

        HWND title = CreateWindowExW(0, L"STATIC", L"正在准备运行环境", WS_CHILD | WS_VISIBLE, 26,
                                     20, kWindowWidth - 52, 32, window, nullptr, g_instance,
                                     nullptr);
        SendMessageW(title, WM_SETFONT, reinterpret_cast<WPARAM>(title_font), TRUE);

        HWND text = CreateWindowExW(0, L"STATIC",
                                    L"首次启动需要解开约 600 MB 的模型和运行库，只需这一次。\r\n"
                                    L"完成之后双击就会直接打开界面。",
                                    WS_CHILD | WS_VISIBLE, 26, 60, kWindowWidth - 52, 48, window,
                                    nullptr, g_instance, nullptr);
        SendMessageW(text, WM_SETFONT, reinterpret_cast<WPARAM>(body_font), TRUE);

        HWND bar = CreateWindowExW(0, PROGRESS_CLASSW, nullptr, WS_CHILD | WS_VISIBLE | PBS_MARQUEE,
                                   26, 122, kWindowWidth - 52, 16, window,
                                   reinterpret_cast<HMENU>(static_cast<INT_PTR>(kIdBar)),
                                   g_instance, nullptr);
        SendMessageW(bar, PBM_SETMARQUEE, TRUE, 40);
        return 0;
    }
    case WM_APP_UNPACKED:
        if (wparam && launch_app()) {
            DestroyWindow(window);
            return 0;
        }
        if (wparam) g_error = L"运行环境已就绪，但启动 nr-app.exe 失败。";
        MessageBoxW(window, g_error.c_str(), L"NR Studio", MB_ICONERROR | MB_OK);
        DestroyWindow(window);
        return 0;
    case WM_CLOSE:
        // The close button is disabled while unpacking; this only fires after.
        DestroyWindow(window);
        return 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    default:
        return DefWindowProcW(window, message, wparam, lparam);
    }
}

}  // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int show) {
    g_instance = instance;

    const std::wstring self = exe_path();
    Footer footer{};
    std::uint64_t file_size = 0;
    if (!read_footer(self, footer, file_size)) {
        MessageBoxW(nullptr,
                    L"这个 exe 不包含运行数据，或者下载不完整。\r\n请重新获取完整的安装文件。",
                    L"NR Studio", MB_ICONERROR | MB_OK);
        return 1;
    }

    const std::string id(footer.id, strnlen(footer.id, sizeof(footer.id)));
    g_target = local_app_data() + L"\\NRStudio\\" + std::wstring(id.begin(), id.end());
    const std::wstring ready = g_target + L"\\.nr_ready";
    if (file_exists(g_target + L"\\nr-app.exe") && file_exists(ready)) {
        // Warm start: no window at all, just hand over to the real program.
        return launch_app() ? 0 : 1;
    }

    trace(L"main: cold start, id " + std::wstring(id.begin(), id.end()));
    trace(L"main: target " + g_target);

    // A second double-click while the first unpack is still running must not
    // start a second unpack into the same directory.
    HANDLE busy = CreateMutexW(nullptr, TRUE, (L"Global\\NRStudioSetup_" +
                                               std::wstring(id.begin(), id.end())).c_str());
    if (busy && GetLastError() == ERROR_ALREADY_EXISTS) {
        trace(L"main: another setup is running");
        CloseHandle(busy);
        return 0;
    }

    INITCOMMONCONTROLSEX controls{sizeof(controls), ICC_PROGRESS_CLASS};
    InitCommonControlsEx(&controls);

    WNDCLASSEXW klass{};
    klass.cbSize = sizeof(klass);
    klass.lpfnWndProc = window_proc;
    klass.hInstance = instance;
    klass.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    klass.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);
    klass.lpszClassName = L"NRStudioLauncher";
    RegisterClassExW(&klass);

    RECT frame{0, 0, kWindowWidth, kWindowHeight};
    AdjustWindowRectEx(&frame, WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU, FALSE, 0);
    const int w = frame.right - frame.left;
    const int h = frame.bottom - frame.top;
    const int x = (GetSystemMetrics(SM_CXSCREEN) - w) / 2;
    const int y = (GetSystemMetrics(SM_CYSCREEN) - h) / 2;

    g_window = CreateWindowExW(0, klass.lpszClassName, L"NR Studio",
                               WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU, x, y, w, h, nullptr,
                               nullptr, instance, nullptr);
    if (!g_window) return 1;
    ShowWindow(g_window, show);
    UpdateWindow(g_window);
    EnableWindow(g_window, FALSE);  // keeps the close button dead while unpacking

    CreateThread(nullptr, 0, unpack_thread, nullptr, 0, nullptr);

    MSG message{};
    while (GetMessageW(&message, nullptr, 0, 0) > 0) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
    CloseHandle(busy);
    return 0;
}
