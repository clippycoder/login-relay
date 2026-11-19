// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using LoginRelay.Core;
using Xunit;

namespace LoginRelay.Core.Tests
{
    /// <summary>Runs every section of the shared tests/vectors.json.</summary>
    public class VectorTests
    {
        private static JsonElement Load()
        {
            string path = Path.Combine(AppContext.BaseDirectory, "vectors.json");
            using (JsonDocument doc = JsonDocument.Parse(File.ReadAllText(path)))
            {
                return doc.RootElement.Clone();
            }
        }

        private static string? Str(JsonElement e, string name)
        {
            if (!e.TryGetProperty(name, out JsonElement v) || v.ValueKind == JsonValueKind.Null)
            {
                return null;
            }
            return v.GetString();
        }

        private static void Finish(List<string> failures, int count)
        {
            Assert.True(count > 0, "the section is empty or missing");
            Assert.True(failures.Count == 0, string.Join(Environment.NewLine, failures));
        }

        [Fact]
        public void Requests()
        {
            JsonElement root = Load();
            var failures = new List<string>();
            int count = 0;
            foreach (JsonElement c in root.GetProperty("requests").EnumerateArray())
            {
                count++;
                string name = Str(c, "name") ?? "?";
                var request = new RelayRequest(Str(c, "url")!, Str(c, "referrer"), Str(c, "label"));
                foreach (JsonProperty expect in c.GetProperty("expect").EnumerateObject())
                {
                    string key = expect.Name;
                    JsonElement want = expect.Value;
                    string? actualText = null;
                    bool? actualBool = null;
                    switch (key)
                    {
                        case "kind": actualText = request.Kind.ToString(); break;
                        case "redirect": actualText = request.RedirectAddress; break;
                        case "state": actualText = request.State; break;
                        case "caller": actualText = request.Caller; break;
                        case "appName": actualText = request.AppName; break;
                        case "catchableScheme": actualText = request.CatchableScheme; break;
                        case "host": actualText = request.Host; break;
                        case "lacksPKCE": actualBool = request.LacksPkce; break;
                        default:
                            failures.Add(name + ": unknown expectation '" + key + "'");
                            continue;
                    }
                    if (want.ValueKind == JsonValueKind.True || want.ValueKind == JsonValueKind.False)
                    {
                        if (actualBool != want.GetBoolean())
                        {
                            failures.Add(name + ": " + key + " expected " + want.GetBoolean() + " but was " + actualBool);
                        }
                    }
                    else if (want.ValueKind == JsonValueKind.Null)
                    {
                        if (actualText != null)
                        {
                            failures.Add(name + ": " + key + " expected absent but was '" + actualText + "'");
                        }
                    }
                    else
                    {
                        string expected = want.GetString()!;
                        bool same = key == "kind"
                            ? string.Equals(expected, actualText, StringComparison.OrdinalIgnoreCase)
                            : expected == actualText;
                        if (!same)
                        {
                            failures.Add(name + ": " + key + " expected '" + expected + "' but was '" + actualText + "'");
                        }
                    }
                }
            }
            Finish(failures, count);
        }

        [Fact]
        public void Accepts()
        {
            JsonElement root = Load();
            var failures = new List<string>();
            int count = 0;
            foreach (JsonElement c in root.GetProperty("accepts").EnumerateArray())
            {
                count++;
                string name = Str(c, "name") ?? "?";
                var request = new RelayRequest(Str(c, "url")!, Str(c, "referrer"), Str(c, "label"));
                CallbackVerdict verdict = request.Accepts(Str(c, "callback")!);
                string expected = Str(c, "expect")!;
                if (!string.Equals(verdict.ToString(), expected, StringComparison.OrdinalIgnoreCase))
                {
                    failures.Add(name + ": expected " + expected + " but was " + verdict);
                }
            }
            Finish(failures, count);
        }

        [Fact]
        public void UrlParams()
        {
            JsonElement root = Load();
            var failures = new List<string>();
            int count = 0;
            foreach (JsonElement c in root.GetProperty("urlParams").EnumerateArray())
            {
                count++;
                string name = Str(c, "name") ?? "?";
                Dictionary<string, string> actual = Redirect.UrlParams(Str(c, "url")!);
                var expected = new Dictionary<string, string>();
                foreach (JsonProperty p in c.GetProperty("expect").EnumerateObject())
                {
                    expected[p.Name] = p.Value.GetString()!;
                }
                bool same = actual.Count == expected.Count
                    && expected.All(kv => actual.TryGetValue(kv.Key, out string? got) && got == kv.Value);
                if (!same)
                {
                    failures.Add(name + ": expected {" + string.Join(", ", expected.Select(kv => kv.Key + "=" + kv.Value))
                        + "} but was {" + string.Join(", ", actual.Select(kv => kv.Key + "=" + kv.Value)) + "}");
                }
            }
            Finish(failures, count);
        }

        [Fact]
        public void ShellQuote()
        {
            JsonElement root = Load();
            var failures = new List<string>();
            int count = 0;
            foreach (JsonElement c in root.GetProperty("shellQuote").EnumerateArray())
            {
                count++;
                string input = c[0].GetString()!;
                string expected = c[1].GetString()!;
                string actual = Redirect.ShellQuote(input);
                if (actual != expected)
                {
                    failures.Add("shellQuote(" + input + "): expected " + expected + " but was " + actual);
                }
            }
            Finish(failures, count);
        }

        [Fact]
        public void VersionLess()
        {
            JsonElement root = Load();
            var failures = new List<string>();
            int count = 0;
            foreach (JsonElement c in root.GetProperty("versionLess").EnumerateArray())
            {
                count++;
                string a = c[0].GetString()!;
                string b = c[1].GetString()!;
                bool expected = c[2].GetBoolean();
                bool actual = Redirect.VersionLess(a, b);
                if (actual != expected)
                {
                    failures.Add("versionLess(" + a + ", " + b + "): expected " + expected + " but was " + actual);
                }
            }
            Finish(failures, count);
        }
    }
}
