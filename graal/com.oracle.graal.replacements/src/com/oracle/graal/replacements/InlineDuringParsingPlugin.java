/*
 * Copyright (c) 2013, 2015, Oracle and/or its affiliates. All rights reserved.
 * DO NOT ALTER OR REMOVE COPYRIGHT NOTICES OR THIS FILE HEADER.
 *
 * This code is free software; you can redistribute it and/or modify it
 * under the terms of the GNU General Public License version 2 only, as
 * published by the Free Software Foundation.
 *
 * This code is distributed in the hope that it will be useful, but WITHOUT
 * ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
 * FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
 * version 2 for more details (a copy is included in the LICENSE file that
 * accompanied this code).
 *
 * You should have received a copy of the GNU General Public License version
 * 2 along with this work; if not, write to the Free Software Foundation,
 * Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA.
 *
 * Please contact Oracle, 500 Oracle Parkway, Redwood Shores, CA 94065 USA
 * or visit www.oracle.com if you need additional information or have any
 * questions.
 */
package com.oracle.graal.replacements;

import static com.oracle.graal.compiler.common.GraalOptions.TrivialInliningSize;
import static com.oracle.graal.java.BytecodeParserOptions.InlineDuringParsingMaxDepth;

import com.oracle.graal.nodes.ValueNode;
import com.oracle.graal.nodes.graphbuilderconf.GraphBuilderContext;
import com.oracle.graal.nodes.graphbuilderconf.InlineInvokePlugin;

import jdk.vm.ci.meta.ResolvedJavaMethod;

public final class InlineDuringParsingPlugin implements InlineInvokePlugin {

    @Override
    public InlineInfo shouldInlineInvoke(GraphBuilderContext b, ResolvedJavaMethod method, ValueNode[] args) {
        // @formatter:off
        if (method.hasBytecodes() &&
            method.getDeclaringClass().isLinked() &&
            method.canBeInlined()) {

            // Test force inlining first
            if (method.shouldBeInlined()) {
                return new InlineInfo(method, false);
            }

            if (!method.isSynchronized() &&
                checkSize(method, args) &&
                b.getDepth() < InlineDuringParsingMaxDepth.getValue()) {
                return new InlineInfo(method, false);
            }
        }
        // @formatter:on
        return null;
    }

    private static boolean checkSize(ResolvedJavaMethod method, ValueNode[] args) {
        int bonus = 1;
        for (ValueNode v : args) {
            if (v.isConstant()) {
                bonus++;
            }
        }
        return method.getCode().length <= TrivialInliningSize.getValue() * bonus;
    }
}
