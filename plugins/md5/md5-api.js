"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.fight = fight;
exports.win_rate = win_rate;
exports.win_rate_callback = win_rate_callback;
exports.score = score;
exports.score_callback = score_callback;
exports.run_any = run_any;
exports.wrap_any = wrap_any;
const fs = require("fs");
const md5_module = require("./md5.js");
/**
 * 对于胜率/评分的输入检查
 * @param names
 * @returns
 */
function test_check(names) {
    return names.trim().startsWith("!test!");
}
/**
 *
 * @param names 原始的输入框输入
 * @returns 对战结果
 */
async function fight(names) {
    if (test_check(names)) {
        throw new Error(`怎么能在对战里有 !test!(恼)\n${names}`);
    }
    return await md5_module.fight(names);
}
/**
 * 测量胜率
 * @param names 原始的输入框输入
 * @param round 战斗的回合数
 * @returns 胜率结果
 */
async function win_rate(names, round) {
    if (round <= 0) {
        throw new Error("round 必须大于 0");
    }
    return await md5_module.win_rate(names, round);
}
/**
 *
 * @param names 原始的输入框输入
 * @param callback 用于接收胜率的回调函数
 * @returns 胜率结果
 */
async function win_rate_callback(names, callback) {
    return await md5_module.win_rate_callback(names, callback);
}
async function score(names, round) {
    if (round <= 0) {
        throw new Error("round 必须大于 0");
    }
    return await md5_module.score(names, round);
}
async function score_callback(names, callback) {
    return await md5_module.score_callback(names, callback);
}
async function run_any(names, round) {
    return await md5_module.run_any(names, round);
}
const out_limit = 1000;
async function wrap_any(names, round) {
    const result = await run_any(names, round);
    if ("message" in result) {
        return `赢家:|${result.source_plr}|`;
    }
    if ("win_count" in result) {
        const win_rate = (result.win_count * 100) / round;
        let output_str = `最终胜率:|${win_rate.toFixed(4)}%|(${round}轮)`;
        if (round > out_limit) {
            const output_datas = [];
            result.raw_data.forEach((data) => {
                if (data.round % out_limit === 0) {
                    output_datas.push(data);
                }
            });
            output_datas.forEach((data) => {
                const win_rate = (data.win_count * 100) / data.round;
                output_str += `\n${win_rate.toFixed(2)}%(${data.round})`;
            });
        }
        return output_str;
    }
    const win_rate = ((result.score * 10000) / round).toFixed(2);
    let output_str = `分数:|${win_rate}|(${round}轮)`;
    if (round > out_limit) {
        const output_datas = [];
        result.raw_data.forEach((data) => {
            if (data.round % out_limit === 0) {
                output_datas.push(data);
            }
        });
        output_datas.forEach((data) => {
            const win_rate = ((data.score / data.round) * 10000).toFixed(2);
            output_str += `\n${win_rate}(${data.round})`;
        });
    }
    return output_str;
}
async function read_stdin() {
    return await new Promise((resolve, reject) => {
        let data = "";
        process.stdin.setEncoding("utf8");
        process.stdin.on("data", (chunk) => {
            data += chunk;
        });
        process.stdin.on("end", () => resolve(data));
        process.stdin.on("error", reject);
    });
}
async function cli() {
    const args = process.argv.slice(2);
    const mode = args[0] || "any";
    const round = Number.parseInt(args[1] || "10000", 10);
    const input = args[2] ? fs.readFileSync(args[2], "utf8") : await read_stdin();
    if (mode === "fight") {
        md5_module.run_env.fight_only = true;
        const result = await fight(input);
        console.log(result.source_plr);
        return;
    }
    if (mode === "score") {
        const result = await score(input, round);
        const win_rate = ((result.score * 10000) / round).toFixed(2);
        console.log(`分数:|${win_rate}|(${round}轮)`);
        return;
    }
    if (mode === "win-rate") {
        const result = await win_rate(input, round);
        const rate = ((result.win_count * 100) / round).toFixed(4);
        console.log(`最终胜率:|${rate}%|(${round}轮)`);
        return;
    }
    console.log(await wrap_any(input, round));
}
if (require.main === module) {
    cli().catch((e) => {
        console.error(e);
        process.exit(1);
    });
}
