// Small, bounded diagnostic: native accessibility presses only pre-payment controls.
import Foundation
import ApplicationServices
import CoreGraphics
func attr(_ e: AXUIElement, _ key: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(e, key as CFString, &value) == .success ? value : nil
}
func text(_ e: AXUIElement, _ key: String) -> String { attr(e,key) as? String ?? "" }
let args=CommandLine.arguments
guard args.count==3, let pid=Int32(args[1]), ["inspect","RETURN TO CHECKOUT","Continue"].contains(args[2]) else {exit(2)}
let app=AXUIElementCreateApplication(pid)
AXUIElementSetAttributeValue(app,"AXEnhancedUserInterface" as CFString,kCFBooleanTrue)
AXUIElementSetAttributeValue(app,"AXManualAccessibility" as CFString,kCFBooleanTrue)
let windows=attr(app,"AXWindows") as? [AXUIElement] ?? []
var buttons:[AXUIElement]=[]
var shippingPage=false
var pageURL=""
var content:[String]=[]
var fields:[String]=[]
var radios:[[String:Any]]=[]
var checks:[[String:Any]]=[]
func walk(_ e:AXUIElement,_ depth:Int) {
    if depth>40 {return}
    if text(e,"AXRole")=="AXButton" {buttons.append(e)}
    if text(e,"AXRole")=="AXStaticText" {
        let value=text(e,"AXValue");if !value.isEmpty {content.append(value)}
    }
    if text(e,"AXRole")=="AXTextField" {fields.append(text(e,"AXTitle")+" "+text(e,"AXDescription"))}
    if text(e,"AXRole")=="AXRadioButton" {radios.append(["label":label(e),"selected":(attr(e,"AXValue") as? NSNumber)?.boolValue ?? false])}
    if text(e,"AXRole")=="AXCheckBox" {checks.append(["label":label(e),"checked":(attr(e,"AXValue") as? NSNumber)?.boolValue ?? false])}
    if text(e,"AXRole")=="AXTextField" && text(e,"AXDescription")=="Address and search bar" {
        let value=text(e,"AXValue")
        pageURL=value
        shippingPage=value.contains("lululemon.com.au/en-au/shop/checkout/shipping")
    }
    for child in attr(e,"AXChildren") as? [AXUIElement] ?? [] {walk(child,depth+1)}
}
for w in windows where text(w,"AXSubrole")=="AXStandardWindow" {
    AXUIElementSetAttributeValue(w,"AXEnhancedUserInterface" as CFString,kCFBooleanTrue)
    walk(w,0)
}
func label(_ e:AXUIElement)->String {
    let title=text(e,"AXTitle");return title.isEmpty ? text(e,"AXDescription") : title
}
if args[2]=="inspect" {
    let all=CGWindowListCopyWindowInfo([.optionAll],kCGNullWindowID) as? [[String:Any]] ?? []
    let candidates=all.filter{($0[kCGWindowOwnerPID as String] as? Int)==Int(pid) && ($0[kCGWindowLayer as String] as? Int)==0}
    let main=candidates.max {a,b in
        let x=a[kCGWindowBounds as String] as? [String:Double] ?? [:]
        let y=b[kCGWindowBounds as String] as? [String:Double] ?? [:]
        return (x["Width"] ?? 0)*(x["Height"] ?? 0) < (y["Width"] ?? 0)*(y["Height"] ?? 0)
    }
    let data=try! JSONSerialization.data(withJSONObject:["url":pageURL,"buttons":buttons.map(label),"texts":content,"field_labels":fields,"radios":radios,"checkboxes":checks,
        "main_window_id":main?[kCGWindowNumber as String] as? Int ?? 0])
    print(String(data:data,encoding:.utf8)!);exit(0)
}
let matches=buttons.filter{label($0)==args[2]}
guard shippingPage else {print("not_shipping_page");exit(5)}
guard matches.count==1 else {print("ambiguous_or_missing_control");exit(3)}
guard AXUIElementPerformAction(matches[0],"AXPress" as CFString) == .success else {print("native_press_failed");exit(4)}
print("pressed_pre_payment_control")
