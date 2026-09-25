// Test double for lgcal/DisplayTool.cs with the same public surface. It
// starts in the state that broke the Panasonic run: the TV duplicated with
// the PC monitor (one shared desktop), with HDR on. Calls are appended to
// the file named by FAKE_DISPLAY_LOG.
using System;
using System.Collections.Generic;
using System.IO;

public static class DisplayTool
{
    public const uint TOPOLOGY_INTERNAL = 1, TOPOLOGY_CLONE = 2, TOPOLOGY_EXTEND = 4, TOPOLOGY_EXTERNAL = 8;
    static uint topology = TOPOLOGY_CLONE;
    static bool hdr = true;

    public class Output
    {
        public string Gdi = ""; public string Name = "";
        public int X, Y, Width, Height;
        public bool HdrSupported, HdrOn; public uint Bits;
        public uint AdapterLow; public int AdapterHigh; public uint TargetId;
    }

    static void Log(string line)
    {
        string path = Environment.GetEnvironmentVariable("FAKE_DISPLAY_LOG");
        if (!string.IsNullOrEmpty(path)) File.AppendAllText(path, line + "\n");
    }

    static Output Make(string gdi, string name, int x, uint target, bool hdrOn)
    {
        Output o = new Output();
        o.Gdi = gdi; o.Name = name; o.X = x; o.Width = 1920; o.Height = 1080;
        o.HdrSupported = target == 2; o.HdrOn = hdrOn; o.Bits = 8; o.AdapterLow = 77; o.TargetId = target;
        return o;
    }

    public static Output[] Outputs()
    {
        if (topology == TOPOLOGY_CLONE)
            return new Output[] { Make("\\\\.\\DISPLAY1", "DELL U2415", 0, 1, false),
                                  Make("\\\\.\\DISPLAY1", "LG TV SSCR2", 0, 2, hdr) };
        return new Output[] { Make("\\\\.\\DISPLAY1", "DELL U2415", 0, 1, false),
                              Make("\\\\.\\DISPLAY2", "LG TV SSCR2", 1920, 2, hdr) };
    }

    public static uint Topology() { return topology; }
    public static int SetTopology(uint t) { Log("topology " + t); topology = t; return 0; }
    public static int SetHdr(uint low, int high, uint target, bool on)
    {
        Log("hdr " + low + " " + target + " " + on); hdr = on; return 0;
    }
}
