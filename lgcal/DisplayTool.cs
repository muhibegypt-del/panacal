// Windows display configuration for the pattern screen, via the documented
// Connecting and Configuring Displays (CCD) API in user32:
//   - the active outputs with their GDI name (\\.\DISPLAY2) and EDID name
//     ("LG TV SSCR2"), desktop position, HDR/advanced-colour state and bit depth;
//   - the current topology (PC only / duplicate / extend / second only) and
//     switching it;
//   - turning HDR (advanced colour) off and back on for one output.
// Loaded by display_setup.ps1 with Add-Type.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class DisplayTool
{
    const uint QDC_ONLY_ACTIVE_PATHS = 0x2;
    const uint QDC_DATABASE_CURRENT = 0x4;
    const uint SDC_APPLY = 0x80;
    const int ERROR_INSUFFICIENT_BUFFER = 122;
    const uint MODE_INFO_TYPE_SOURCE = 1;
    const uint GET_SOURCE_NAME = 1, GET_TARGET_NAME = 2, GET_ADVANCED_COLOR_INFO = 9, SET_ADVANCED_COLOR_STATE = 10;
    public const uint TOPOLOGY_INTERNAL = 1, TOPOLOGY_CLONE = 2, TOPOLOGY_EXTEND = 4, TOPOLOGY_EXTERNAL = 8;

    [StructLayout(LayoutKind.Sequential)]
    public struct LUID { public uint LowPart; public int HighPart; }

    [StructLayout(LayoutKind.Sequential)]
    struct PATH_SOURCE_INFO { public LUID adapterId; public uint id; public uint modeInfoIdx; public uint statusFlags; }

    [StructLayout(LayoutKind.Sequential)]
    struct PATH_TARGET_INFO
    {
        public LUID adapterId; public uint id; public uint modeInfoIdx; public uint outputTechnology;
        public uint rotation; public uint scaling; public uint refreshNumerator; public uint refreshDenominator;
        public uint scanLineOrdering; public int targetAvailable; public uint statusFlags;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct PATH_INFO { public PATH_SOURCE_INFO sourceInfo; public PATH_TARGET_INFO targetInfo; public uint flags; }

    // DISPLAYCONFIG_MODE_INFO: 16-byte header and a 48-byte union. For a
    // source mode the union starts width, height, pixelFormat, position.x,
    // position.y (all 32-bit).
    [StructLayout(LayoutKind.Sequential)]
    struct MODE_INFO
    {
        public uint infoType; public uint id; public LUID adapterId;
        public ulong u0, u1, u2, u3, u4, u5;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct HEADER { public uint type; public uint size; public LUID adapterId; public uint id; }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct SOURCE_DEVICE_NAME
    {
        public HEADER header;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string viewGdiDeviceName;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct TARGET_DEVICE_NAME
    {
        public HEADER header; public uint flags; public uint outputTechnology;
        public ushort edidManufactureId; public ushort edidProductCodeId; public uint connectorInstance;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string monitorFriendlyDeviceName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string monitorDevicePath;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct ADVANCED_COLOR_INFO { public HEADER header; public uint value; public uint colorEncoding; public uint bitsPerColorChannel; }

    [StructLayout(LayoutKind.Sequential)]
    struct SET_ADVANCED_COLOR { public HEADER header; public uint value; }

    [DllImport("user32.dll")]
    static extern int GetDisplayConfigBufferSizes(uint flags, out uint numPaths, out uint numModes);
    [DllImport("user32.dll")]
    static extern int QueryDisplayConfig(uint flags, ref uint numPaths, [Out] PATH_INFO[] paths,
                                         ref uint numModes, [Out] MODE_INFO[] modes, IntPtr topologyId);
    [DllImport("user32.dll", EntryPoint = "QueryDisplayConfig")]
    static extern int QueryDisplayConfigTopology(uint flags, ref uint numPaths, [Out] PATH_INFO[] paths,
                                                 ref uint numModes, [Out] MODE_INFO[] modes, out uint topologyId);
    [DllImport("user32.dll")]
    static extern int DisplayConfigGetDeviceInfo(ref SOURCE_DEVICE_NAME info);
    [DllImport("user32.dll")]
    static extern int DisplayConfigGetDeviceInfo(ref TARGET_DEVICE_NAME info);
    [DllImport("user32.dll")]
    static extern int DisplayConfigGetDeviceInfo(ref ADVANCED_COLOR_INFO info);
    [DllImport("user32.dll")]
    static extern int DisplayConfigSetDeviceInfo(ref SET_ADVANCED_COLOR info);
    [DllImport("user32.dll")]
    static extern int SetDisplayConfig(uint numPaths, IntPtr paths, uint numModes, IntPtr modes, uint flags);

    public class Output
    {
        public string Gdi = ""; public string Name = "";
        public int X, Y, Width, Height;
        public bool HdrSupported, HdrOn; public uint Bits;
        public uint AdapterLow; public int AdapterHigh; public uint TargetId;
    }

    static HEADER Header(uint type, int size, LUID adapter, uint id)
    {
        HEADER h = new HEADER();
        h.type = type; h.size = (uint)size; h.adapterId = adapter; h.id = id;
        return h;
    }

    static int Query(uint flags, out PATH_INFO[] paths, out MODE_INFO[] modes, out uint topology)
    {
        topology = 0;
        for (int attempt = 0; attempt < 5; attempt++)
        {
            uint np, nm;
            int r = GetDisplayConfigBufferSizes(flags, out np, out nm);
            if (r != 0) { paths = new PATH_INFO[0]; modes = new MODE_INFO[0]; return r; }
            paths = new PATH_INFO[np];
            modes = new MODE_INFO[nm];
            r = flags == QDC_DATABASE_CURRENT
                ? QueryDisplayConfigTopology(flags, ref np, paths, ref nm, modes, out topology)
                : QueryDisplayConfig(flags, ref np, paths, ref nm, modes, IntPtr.Zero);
            if (r == ERROR_INSUFFICIENT_BUFFER) continue;   // displays changed between the calls
            Array.Resize(ref paths, (int)np);
            Array.Resize(ref modes, (int)nm);
            return r;
        }
        paths = new PATH_INFO[0]; modes = new MODE_INFO[0];
        return ERROR_INSUFFICIENT_BUFFER;
    }

    public static Output[] Outputs()
    {
        PATH_INFO[] paths; MODE_INFO[] modes; uint unused;
        int r = Query(QDC_ONLY_ACTIVE_PATHS, out paths, out modes, out unused);
        if (r != 0) throw new InvalidOperationException("QueryDisplayConfig failed: " + r);
        List<Output> list = new List<Output>();
        foreach (PATH_INFO path in paths)
        {
            Output o = new Output();
            SOURCE_DEVICE_NAME source = new SOURCE_DEVICE_NAME();
            source.header = Header(GET_SOURCE_NAME, Marshal.SizeOf(typeof(SOURCE_DEVICE_NAME)),
                                   path.sourceInfo.adapterId, path.sourceInfo.id);
            if (DisplayConfigGetDeviceInfo(ref source) == 0) o.Gdi = source.viewGdiDeviceName;
            TARGET_DEVICE_NAME target = new TARGET_DEVICE_NAME();
            target.header = Header(GET_TARGET_NAME, Marshal.SizeOf(typeof(TARGET_DEVICE_NAME)),
                                   path.targetInfo.adapterId, path.targetInfo.id);
            if (DisplayConfigGetDeviceInfo(ref target) == 0) o.Name = target.monitorFriendlyDeviceName ?? "";
            ADVANCED_COLOR_INFO color = new ADVANCED_COLOR_INFO();
            color.header = Header(GET_ADVANCED_COLOR_INFO, Marshal.SizeOf(typeof(ADVANCED_COLOR_INFO)),
                                  path.targetInfo.adapterId, path.targetInfo.id);
            if (DisplayConfigGetDeviceInfo(ref color) == 0)
            {
                o.HdrSupported = (color.value & 0x1) != 0;
                o.HdrOn = (color.value & 0x2) != 0;
                o.Bits = color.bitsPerColorChannel;
            }
            uint index = path.sourceInfo.modeInfoIdx;
            if (index < modes.Length && modes[index].infoType == MODE_INFO_TYPE_SOURCE)
            {
                MODE_INFO m = modes[index];
                o.Width = (int)(uint)(m.u0 & 0xFFFFFFFF);
                o.Height = (int)(uint)(m.u0 >> 32);
                o.X = (int)(uint)(m.u1 >> 32);
                o.Y = (int)(uint)(m.u2 & 0xFFFFFFFF);
            }
            o.AdapterLow = path.targetInfo.adapterId.LowPart;
            o.AdapterHigh = path.targetInfo.adapterId.HighPart;
            o.TargetId = path.targetInfo.id;
            list.Add(o);
        }
        return list.ToArray();
    }

    public static uint Topology()
    {
        PATH_INFO[] paths; MODE_INFO[] modes; uint topology;
        return Query(QDC_DATABASE_CURRENT, out paths, out modes, out topology) == 0 ? topology : 0;
    }

    // Same effect as DisplaySwitch.exe /internal /clone /extend /external.
    public static int SetTopology(uint topology)
    {
        return SetDisplayConfig(0, IntPtr.Zero, 0, IntPtr.Zero, SDC_APPLY | topology);
    }

    public static int SetHdr(uint adapterLow, int adapterHigh, uint targetId, bool on)
    {
        LUID adapter = new LUID();
        adapter.LowPart = adapterLow; adapter.HighPart = adapterHigh;
        SET_ADVANCED_COLOR state = new SET_ADVANCED_COLOR();
        state.header = Header(SET_ADVANCED_COLOR_STATE, Marshal.SizeOf(typeof(SET_ADVANCED_COLOR)), adapter, targetId);
        state.value = on ? 1u : 0u;
        return DisplayConfigSetDeviceInfo(ref state);
    }

    // Struct sizes the API requires; checked by the offline tests.
    public static string Sizes()
    {
        return Marshal.SizeOf(typeof(PATH_INFO)) + "," + Marshal.SizeOf(typeof(MODE_INFO)) + "," +
               Marshal.SizeOf(typeof(SOURCE_DEVICE_NAME)) + "," + Marshal.SizeOf(typeof(TARGET_DEVICE_NAME)) + "," +
               Marshal.SizeOf(typeof(ADVANCED_COLOR_INFO)) + "," + Marshal.SizeOf(typeof(SET_ADVANCED_COLOR));
    }
}
